from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import inspect
import json
import os
from pathlib import Path
import random
import time
from typing import Any, Callable, Protocol

from .astrbot_source import OneBotActionError
from .db import EssenceRepository, SyncState
from .plugin_service import (
    GroupEssencePluginService,
    PluginServiceError,
    RuntimeStatusReport,
    SyncReport,
)


class RuntimeLogger(Protocol):
    def info(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...

    def error(self, message: str) -> None: ...


@dataclass(frozen=True)
class RuntimeConfig:
    scheduled_sync_enabled: bool
    scheduled_sync_block_reason: str
    automatic_backups_enabled: bool
    group_ids: tuple[str, ...]
    admin_ids: tuple[str, ...]
    interval_seconds: float
    startup_delay_seconds: float
    timeout_seconds: float
    jitter_percent: int
    failure_threshold: int
    retry_base_seconds: float
    max_backoff_seconds: float
    failure_alerts_enabled: bool
    backup_interval_seconds: float
    backup_keep_daily: int
    backup_keep_weekly: int

    @classmethod
    def from_settings(cls, settings: Any) -> RuntimeConfig:
        validation_mode = bool(settings.validation_mode)
        return cls(
            scheduled_sync_enabled=bool(settings.enable_scheduled_sync),
            scheduled_sync_block_reason=str(
                settings.scheduled_sync_block_reason or ""
            ),
            automatic_backups_enabled=(
                bool(settings.enable_automatic_backups) and not validation_mode
            ),
            group_ids=tuple(sorted(settings.allowed_group_ids)),
            admin_ids=tuple(sorted(settings.admin_ids)),
            interval_seconds=float(settings.scheduled_sync_interval_minutes * 60),
            startup_delay_seconds=float(
                settings.scheduled_sync_startup_delay_seconds
            ),
            timeout_seconds=float(settings.scheduled_sync_timeout_seconds),
            jitter_percent=int(settings.scheduled_sync_jitter_percent),
            failure_threshold=int(settings.scheduled_sync_failure_threshold),
            retry_base_seconds=float(settings.scheduled_sync_retry_base_seconds),
            max_backoff_seconds=float(
                settings.scheduled_sync_max_backoff_minutes * 60
            ),
            failure_alerts_enabled=bool(settings.enable_failure_alerts),
            backup_interval_seconds=float(settings.backup_interval_hours * 3600),
            backup_keep_daily=int(settings.backup_keep_daily),
            backup_keep_weekly=int(settings.backup_keep_weekly),
        )

    @property
    def sync_can_run(self) -> bool:
        return self.scheduled_sync_enabled and not self.scheduled_sync_block_reason

    @property
    def has_background_work(self) -> bool:
        return self.sync_can_run or self.automatic_backups_enabled


class GroupEssenceRuntime:
    """Own one supervised background task for sync, backup, and health state."""

    def __init__(
        self,
        *,
        service: GroupEssencePluginService,
        repository: EssenceRepository,
        action_context: Any,
        config: RuntimeConfig,
        logger: RuntimeLogger,
        health_path: Path,
        random_source: random.Random | None = None,
        now_provider: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.service = service
        self.repository = repository
        self.action_context = action_context
        self.config = config
        self.logger = logger
        self.health_path = health_path
        self.random_source = random_source or random.Random()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self._lifecycle_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._states: dict[str, SyncState] = {}
        self._next_sync_due: dict[str, float] = {}
        self._next_backup_due: float | None = None
        self._last_backup_at = repository.latest_managed_backup_at()
        self._runtime_block_reason = config.scheduled_sync_block_reason

    @property
    def task_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        async with self._lifecycle_lock:
            return await self._start_locked()

    async def _start_locked(self) -> bool:
        if self.task_running:
            return False
        if not self.config.has_background_work:
            return False

        self._runtime_block_reason = self.config.scheduled_sync_block_reason
        current_monotonic = self.monotonic()
        current_time = self._now()
        try:
            await asyncio.to_thread(self.repository.init_db)
            await asyncio.to_thread(self.repository.clear_stale_running_states)
            states = await asyncio.to_thread(
                self.repository.list_sync_states,
                self.config.group_ids,
            )
            self._states = {state.group_id: state for state in states}
            self._next_sync_due = {}
            if self.config.sync_can_run:
                for group_id in self.config.group_ids:
                    state = self._states[group_id]
                    delay = self._initial_sync_delay(state, current_time)
                    scheduled_state = replace(
                        state,
                        next_run_at=_format_utc(
                            current_time + timedelta(seconds=delay)
                        ),
                        running=False,
                        updated_at=_format_utc(current_time),
                    )
                    self._states[group_id] = scheduled_state
                    self._next_sync_due[group_id] = current_monotonic + delay
                    await asyncio.to_thread(
                        self.repository.save_sync_state,
                        scheduled_state,
                    )
        except Exception as exc:
            self._runtime_block_reason = "database_init_failed"
            self.logger.error(
                "GroupEssence 后台任务未启动："
                f"category={type(exc).__name__}"
            )
            return False

        self._stop_event = asyncio.Event()
        self._next_backup_due = None
        if self.config.automatic_backups_enabled:
            self._next_backup_due = (
                current_monotonic + self._initial_backup_delay()
            )
        self._task = asyncio.create_task(
            self._run_loop(),
            name="group-essence-runtime",
        )
        await self._write_health()
        self.logger.info("GroupEssence 后台任务已启动。")
        return True

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        task = self._task
        self._task = None
        self._stop_event.set()
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._write_health()

    def snapshot(self) -> RuntimeStatusReport:
        states = list(self._states.values())
        last_successes = [state.last_success_at for state in states if state.last_success_at]
        next_runs = [state.next_run_at for state in states if state.next_run_at]
        worst = max(states, key=lambda state: state.consecutive_failures, default=None)
        return RuntimeStatusReport(
            scheduled_sync_enabled=self.config.scheduled_sync_enabled,
            task_running=self.task_running,
            blocked_reason=self._runtime_block_reason,
            last_success_at=max(last_successes, default=""),
            next_run_at=min(next_runs, default=""),
            consecutive_failures=(worst.consecutive_failures if worst else 0),
            last_error_category=(worst.last_error_category if worst else ""),
            automatic_backups_enabled=self.config.automatic_backups_enabled,
            last_backup_at=self._last_backup_at,
        )

    async def run_group_once(self, group_id: str) -> bool:
        normalized_group_id = str(group_id or "").strip()
        if normalized_group_id not in self.config.group_ids:
            raise ValueError("scheduled group is not authorized")
        state = self._states.get(normalized_group_id)
        if state is None:
            state = await asyncio.to_thread(
                self.repository.get_sync_state,
                normalized_group_id,
            )
            self._states[normalized_group_id] = state
        return await self._run_group(normalized_group_id, state)

    async def run_backup_once(self) -> bool:
        if not self.repository.db_path.is_file():
            self._schedule_next_backup(self.config.backup_interval_seconds)
            return False
        try:
            path = await asyncio.to_thread(
                self.repository.create_backup,
                reason="scheduled",
                managed=True,
                now=self._now(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(
                "GroupEssence 自动备份失败："
                f"category={type(exc).__name__}"
            )
            retry_delay = min(
                self.config.backup_interval_seconds,
                max(300.0, self.config.retry_base_seconds),
            )
            self._schedule_next_backup(retry_delay)
            await self._write_health()
            return False

        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        self._last_backup_at = _format_utc(modified)
        retention_ok = True
        try:
            await asyncio.to_thread(
                self.repository.prune_managed_backups,
                keep_daily=self.config.backup_keep_daily,
                keep_weekly=self.config.backup_keep_weekly,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retention_ok = False
            self.logger.warning(
                "GroupEssence 备份轮换失败："
                f"category={type(exc).__name__}"
            )
        self._schedule_next_backup(self.config.backup_interval_seconds)
        await self._write_health()
        self.logger.info("GroupEssence 自动备份完成。")
        return retention_ok

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                ran_work = False
                current = self.monotonic()
                for group_id in sorted(self._next_sync_due):
                    if self._next_sync_due[group_id] > current:
                        continue
                    await self.run_group_once(group_id)
                    ran_work = True
                    if self._stop_event.is_set():
                        return
                if (
                    self._next_backup_due is not None
                    and self._next_backup_due <= self.monotonic()
                ):
                    await self.run_backup_once()
                    ran_work = True
                if ran_work:
                    self._runtime_block_reason = (
                        self.config.scheduled_sync_block_reason
                    )
                    continue

                deadlines = list(self._next_sync_due.values())
                if self._next_backup_due is not None:
                    deadlines.append(self._next_backup_due)
                if not deadlines:
                    return
                wait_seconds = max(0.05, min(deadlines) - self.monotonic())
                self._runtime_block_reason = (
                    self.config.scheduled_sync_block_reason
                )
                if await self._wait_for_stop(wait_seconds):
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.error(
                    "GroupEssence 后台任务发生异常，将继续重试："
                    f"category={type(exc).__name__}"
                )
                self._runtime_block_reason = "runtime_error"
                await self._write_health()
                retry_delay = max(
                    0.05,
                    min(self.config.retry_base_seconds, 60.0),
                )
                if await self._wait_for_stop(retry_delay):
                    return

    async def _run_group(self, group_id: str, state: SyncState) -> bool:
        started_at = self._now()
        started_monotonic = self.monotonic()
        state = replace(
            state,
            last_started_at=_format_utc(started_at),
            running=True,
            updated_at=_format_utc(started_at),
        )
        await self._save_state(state)

        try:
            report = await asyncio.wait_for(
                self.service.sync(self.action_context, group_id),
                timeout=max(0.01, self.config.timeout_seconds),
            )
        except asyncio.CancelledError:
            finished_at = self._now()
            cancelled_state = replace(
                state,
                last_finished_at=_format_utc(finished_at),
                running=False,
                updated_at=_format_utc(finished_at),
            )
            await self._save_state(cancelled_state)
            raise
        except Exception as exc:
            return await self._record_failure(
                group_id,
                state,
                exc,
                started_monotonic,
            )
        return await self._record_success(
            group_id,
            state,
            report,
            started_monotonic,
        )

    async def _record_failure(
        self,
        group_id: str,
        state: SyncState,
        exc: Exception,
        started_monotonic: float,
    ) -> bool:
        finished_at = self._now()
        failure_count = state.consecutive_failures + 1
        delay = self._failure_delay(failure_count)
        next_run_at = finished_at + timedelta(seconds=delay)
        category = _error_category(exc)
        failed_state = replace(
            state,
            last_finished_at=_format_utc(finished_at),
            next_run_at=_format_utc(next_run_at),
            consecutive_failures=failure_count,
            last_error_category=category,
            duration_ms=_duration_ms(self.monotonic() - started_monotonic),
            running=False,
            updated_at=_format_utc(finished_at),
        )
        await self._save_state(failed_state)
        self._next_sync_due[group_id] = self.monotonic() + delay
        if (
            failure_count >= self.config.failure_threshold
            and failed_state.alert_state != "failure"
        ):
            delivered = await self._send_alert(
                "GE 自动同步异常\n"
                f"连续失败：{failure_count}\n"
                f"错误类别：{category}\n"
                "任务将继续退避重试。"
            )
            if delivered:
                failed_state = replace(failed_state, alert_state="failure")
                await self._save_state(failed_state)
        self.logger.warning(
            "GroupEssence 自动同步失败："
            f"category={category}, consecutive={failure_count}"
        )
        await self._write_health()
        return False

    async def _record_success(
        self,
        group_id: str,
        state: SyncState,
        report: SyncReport,
        started_monotonic: float,
    ) -> bool:
        finished_at = self._now()
        delay = self._success_delay()
        next_run_at = finished_at + timedelta(seconds=delay)
        should_send_recovery = state.alert_state == "failure"
        successful_state = replace(
            state,
            last_finished_at=_format_utc(finished_at),
            last_success_at=_format_utc(finished_at),
            next_run_at=_format_utc(next_run_at),
            consecutive_failures=0,
            last_error_category="",
            last_collected=report.collected,
            last_inserted=report.inserted,
            last_updated=report.updated,
            last_refreshed=report.refreshed,
            duration_ms=_duration_ms(self.monotonic() - started_monotonic),
            running=False,
            alert_state=state.alert_state,
            updated_at=_format_utc(finished_at),
        )
        await self._save_state(successful_state)
        self._next_sync_due[group_id] = self.monotonic() + delay
        if should_send_recovery:
            delivered = await self._send_alert(
                "GE 自动同步已恢复\n"
                f"本次采集：{report.collected}\n"
                "连续失败计数已清零。"
            )
            if delivered or not self.config.failure_alerts_enabled:
                successful_state = replace(
                    successful_state,
                    alert_state="healthy",
                )
                await self._save_state(successful_state)
        self.logger.info(
            "GroupEssence 自动同步完成："
            f"collected={report.collected}, inserted={report.inserted}, "
            f"updated={report.updated}, refreshed={report.refreshed}"
        )
        await self._write_health()
        return True

    async def _save_state(self, state: SyncState) -> None:
        await asyncio.to_thread(self.repository.save_sync_state, state)
        self._states[state.group_id] = state

    async def _send_alert(self, message: str) -> bool:
        if not self.config.failure_alerts_enabled:
            return False
        sender = getattr(self.action_context, "send_private_text", None)
        if not callable(sender):
            self.logger.warning("GroupEssence 告警未发送：category=no_alert_gateway")
            return False
        delivered = False
        for admin_id in self.config.admin_ids:
            try:
                result = sender(admin_id, message)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(
                        result,
                        timeout=min(
                            30.0,
                            max(0.05, self.config.timeout_seconds),
                        ),
                    )
                delivered = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(
                    "GroupEssence 告警发送失败："
                    f"category={type(exc).__name__}"
                )
        return delivered

    async def _write_health(self) -> None:
        if not self.config.has_background_work:
            return
        snapshot = self.snapshot()
        payload = {
            "status": _health_status(snapshot),
            "schema": 1,
            "scheduled_sync_enabled": snapshot.scheduled_sync_enabled,
            "task_running": snapshot.task_running,
            "blocked_reason": snapshot.blocked_reason,
            "authorized_group_count": len(self.config.group_ids),
            "last_success_at": snapshot.last_success_at,
            "next_run_at": snapshot.next_run_at,
            "consecutive_failures": snapshot.consecutive_failures,
            "last_error_category": snapshot.last_error_category,
            "automatic_backups_enabled": snapshot.automatic_backups_enabled,
            "last_backup_at": snapshot.last_backup_at,
            "updated_at": _format_utc(self._now()),
        }
        try:
            await asyncio.to_thread(_atomic_write_json, self.health_path, payload)
        except Exception as exc:
            self.logger.warning(
                "GroupEssence 健康快照写入失败："
                f"category={type(exc).__name__}"
            )

    async def _wait_for_stop(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(0.0, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    def _failure_delay(self, failure_count: int) -> float:
        exponent = min(max(0, failure_count - 1), 20)
        delay = min(
            self.config.retry_base_seconds * (2**exponent),
            self.config.max_backoff_seconds,
        )
        if failure_count >= self.config.failure_threshold:
            delay = max(delay, self.config.interval_seconds)
        return max(0.05, delay)

    def _success_delay(self) -> float:
        jitter = max(0, min(self.config.jitter_percent, 30)) / 100
        factor = 1 + self.random_source.uniform(-jitter, jitter)
        return max(0.05, self.config.interval_seconds * factor)

    def _schedule_next_backup(self, delay: float) -> None:
        self._next_backup_due = self.monotonic() + max(0.05, delay)

    def _initial_backup_delay(self) -> float:
        startup_delay = max(0.0, self.config.startup_delay_seconds)
        last_backup = _parse_utc(self._last_backup_at)
        if last_backup is None:
            return startup_delay
        elapsed = max(0.0, (self._now() - last_backup).total_seconds())
        remaining = max(0.0, self.config.backup_interval_seconds - elapsed)
        return max(startup_delay, remaining)

    def _initial_sync_delay(
        self,
        state: SyncState,
        current_time: datetime,
    ) -> float:
        startup_delay = max(0.0, self.config.startup_delay_seconds)
        next_run = _parse_utc(state.next_run_at)
        if next_run is None:
            return startup_delay
        remaining = max(0.0, (next_run - current_time).total_seconds())
        return max(startup_delay, remaining)

    def _now(self) -> datetime:
        value = self.now_provider()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _error_category(exc: Exception) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, OneBotActionError):
        return f"onebot_{exc.action}_{exc.status}"[:64]
    if isinstance(exc, PluginServiceError):
        return str(exc.category or "plugin_service_error")[:64]
    return type(exc).__name__[:64]


def _duration_ms(seconds: float) -> int:
    return max(0, min(int(max(0.0, seconds) * 1000), 2_147_483_647))


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_utc(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _health_status(snapshot: RuntimeStatusReport) -> str:
    if snapshot.blocked_reason and snapshot.blocked_reason != "disabled":
        return "blocked"
    if snapshot.consecutive_failures:
        return "degraded"
    if snapshot.task_running:
        return "ok"
    return "stopped"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()

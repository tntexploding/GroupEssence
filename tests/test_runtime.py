from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from group_essence_extractor.db import EssenceRepository, SyncState
from group_essence_extractor.plugin_service import SyncReport
from group_essence_extractor.runtime import GroupEssenceRuntime, RuntimeConfig


class FakeLogger:
    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []
        self.error_messages: list[str] = []

    def info(self, message: str) -> None:
        self.info_messages.append(message)

    def warning(self, message: str) -> None:
        self.warning_messages.append(message)

    def error(self, message: str) -> None:
        self.error_messages.append(message)


class FakeGateway:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_private_text(self, user_id: str, message: str) -> None:
        self.messages.append((user_id, message))


class SlowGateway(FakeGateway):
    async def send_private_text(self, user_id: str, message: str) -> None:
        await asyncio.sleep(0.2)
        await super().send_private_text(user_id, message)


class FakeService:
    def __init__(self, outcomes: list[object] | None = None) -> None:
        self.outcomes = list(outcomes or [_success_report()])
        self.calls: list[str] = []

    async def sync(self, _: object, group_id: str) -> SyncReport:
        self.calls.append(group_id)
        outcome = self.outcomes.pop(0) if self.outcomes else _success_report()
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, float):
            await asyncio.sleep(outcome)
            return _success_report()
        return outcome  # type: ignore[return-value]


def _success_report() -> SyncReport:
    return SyncReport(
        collected=2,
        inserted=1,
        updated=0,
        refreshed=1,
        unchanged=0,
    )


def _runtime_config(**overrides: object) -> RuntimeConfig:
    values: dict[str, object] = {
        "scheduled_sync_enabled": True,
        "scheduled_sync_block_reason": "",
        "automatic_backups_enabled": False,
        "group_ids": ("123456",),
        "admin_ids": ("admin-1",),
        "interval_seconds": 3600.0,
        "startup_delay_seconds": 3600.0,
        "timeout_seconds": 1.0,
        "jitter_percent": 0,
        "failure_threshold": 2,
        "retry_base_seconds": 0.05,
        "max_backoff_seconds": 3600.0,
        "failure_alerts_enabled": True,
        "backup_interval_seconds": 86400.0,
        "backup_keep_daily": 7,
        "backup_keep_weekly": 4,
    }
    values.update(overrides)
    return RuntimeConfig(**values)  # type: ignore[arg-type]


def _make_runtime(
    temp_root: Path,
    *,
    service: FakeService | None = None,
    gateway: FakeGateway | None = None,
    config: RuntimeConfig | None = None,
    logger: FakeLogger | None = None,
) -> tuple[GroupEssenceRuntime, EssenceRepository, FakeGateway, FakeLogger]:
    repository = EssenceRepository(
        temp_root / "group_essence.db",
        backup_dir=temp_root / "backups",
    )
    selected_gateway = gateway or FakeGateway()
    selected_logger = logger or FakeLogger()
    runtime = GroupEssenceRuntime(
        service=service or FakeService(),  # type: ignore[arg-type]
        repository=repository,
        action_context=selected_gateway,
        config=config or _runtime_config(),
        logger=selected_logger,
        health_path=temp_root / "ge_health.json",
    )
    return runtime, repository, selected_gateway, selected_logger


class GroupEssenceRuntimeTests(unittest.TestCase):
    def test_start_is_idempotent_and_stop_cancels_the_owned_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, repository, _, _ = _make_runtime(root)

            async def scenario() -> tuple[bool, bool, bool, bool]:
                first = await runtime.start()
                running_after_start = runtime.task_running
                second = await runtime.start()
                await runtime.stop()
                return first, running_after_start, second, runtime.task_running

            result = asyncio.run(scenario())

            self.assertEqual(result, (True, True, False, False))
            self.assertTrue(repository.db_path.is_file())
            health = json.loads((root / "ge_health.json").read_text("utf-8"))
            self.assertFalse(health["task_running"])

    def test_concurrent_start_calls_create_exactly_one_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime, _, _, _ = _make_runtime(Path(temp))

            async def scenario() -> tuple[list[bool], bool]:
                starts = await asyncio.gather(runtime.start(), runtime.start())
                running = runtime.task_running
                await runtime.stop()
                return starts, running

            starts, running = asyncio.run(scenario())

            self.assertEqual(starts.count(True), 1)
            self.assertEqual(starts.count(False), 1)
            self.assertTrue(running)

    def test_restart_preserves_persisted_backoff_deadline_and_failure_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, repository, _, _ = _make_runtime(
                root,
                config=replace(_runtime_config(), startup_delay_seconds=0.0),
            )
            repository.init_db()
            next_run = datetime.now(timezone.utc) + timedelta(hours=2)
            repository.save_sync_state(
                SyncState(
                    group_id="123456",
                    next_run_at=next_run.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    consecutive_failures=4,
                    last_error_category="timeout",
                )
            )

            async def scenario() -> tuple[float, int, str]:
                self.assertTrue(await runtime.start())
                remaining = runtime._next_sync_due["123456"] - runtime.monotonic()
                snapshot = runtime.snapshot()
                await runtime.stop()
                return (
                    remaining,
                    snapshot.consecutive_failures,
                    snapshot.last_error_category,
                )

            remaining, failures, category = asyncio.run(scenario())

            self.assertGreater(remaining, 7100)
            self.assertEqual((failures, category), (4, "timeout"))

    def test_success_persists_aggregate_state_and_redacted_health(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, repository, _, _ = _make_runtime(root)
            repository.init_db()

            self.assertTrue(asyncio.run(runtime.run_group_once("123456")))

            state = repository.get_sync_state("123456")
            self.assertEqual(state.consecutive_failures, 0)
            self.assertEqual(state.last_collected, 2)
            self.assertEqual(state.last_inserted, 1)
            self.assertEqual(state.last_refreshed, 1)
            self.assertTrue(state.last_success_at)
            health_text = (root / "ge_health.json").read_text("utf-8")
            health = json.loads(health_text)
            self.assertEqual(health["authorized_group_count"], 1)
            self.assertNotIn("123456", health_text)
            self.assertNotIn("admin-1", health_text)

    def test_failure_threshold_alerts_once_and_success_sends_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = FakeService(
                [RuntimeError("secret-one"), RuntimeError("secret-two"), _success_report()]
            )
            runtime, repository, gateway, _ = _make_runtime(root, service=service)
            repository.init_db()

            async def scenario() -> tuple[bool, bool, bool]:
                return (
                    await runtime.run_group_once("123456"),
                    await runtime.run_group_once("123456"),
                    await runtime.run_group_once("123456"),
                )

            self.assertEqual(asyncio.run(scenario()), (False, False, True))
            self.assertEqual(len(gateway.messages), 2)
            self.assertIn("连续失败：2", gateway.messages[0][1])
            self.assertIn("已恢复", gateway.messages[1][1])
            self.assertNotIn("secret-one", gateway.messages[0][1])
            state = repository.get_sync_state("123456")
            self.assertEqual(state.consecutive_failures, 0)
            self.assertEqual(state.alert_state, "healthy")

    def test_timeout_is_classified_without_exposing_exception_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, repository, _, _ = _make_runtime(
                root,
                service=FakeService([0.1]),
                config=replace(_runtime_config(), timeout_seconds=0.01),
            )
            repository.init_db()

            self.assertFalse(asyncio.run(runtime.run_group_once("123456")))

            state = repository.get_sync_state("123456")
            self.assertEqual(state.last_error_category, "timeout")
            self.assertEqual(state.consecutive_failures, 1)

    def test_alert_timeout_does_not_block_failure_state_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logger = FakeLogger()
            runtime, repository, gateway, _ = _make_runtime(
                root,
                service=FakeService(
                    [
                        RuntimeError("private detail one"),
                        RuntimeError("private detail two"),
                    ]
                ),
                gateway=SlowGateway(),
                config=replace(
                    _runtime_config(),
                    timeout_seconds=0.01,
                    failure_threshold=1,
                ),
                logger=logger,
            )
            repository.init_db()
            recovered_gateway = FakeGateway()

            async def scenario() -> tuple[bool, bool]:
                first = await runtime.run_group_once("123456")
                runtime.action_context = recovered_gateway
                second = await runtime.run_group_once("123456")
                return first, second

            self.assertEqual(asyncio.run(scenario()), (False, False))
            self.assertEqual(gateway.messages, [])
            self.assertEqual(len(recovered_gateway.messages), 1)
            self.assertTrue(
                any("category=TimeoutError" in item for item in logger.warning_messages)
            )
            self.assertEqual(
                repository.get_sync_state("123456").alert_state,
                "failure",
            )

    def test_automatic_backup_is_online_verified_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, repository, _, _ = _make_runtime(
                root,
                config=replace(
                    _runtime_config(),
                    automatic_backups_enabled=True,
                ),
            )
            repository.init_db()

            self.assertTrue(asyncio.run(runtime.run_backup_once()))

            backups = list((root / "backups").glob("scheduled-*.db"))
            self.assertEqual(len(backups), 1)
            self.assertTrue(runtime.snapshot().last_backup_at)
            health = json.loads((root / "ge_health.json").read_text("utf-8"))
            self.assertTrue(health["last_backup_at"])

    def test_retention_failure_does_not_create_rapid_duplicate_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, repository, _, logger = _make_runtime(
                root,
                config=replace(
                    _runtime_config(),
                    automatic_backups_enabled=True,
                ),
            )
            repository.init_db()

            with patch.object(
                repository,
                "prune_managed_backups",
                side_effect=PermissionError("private path"),
            ):
                result = asyncio.run(runtime.run_backup_once())

            self.assertFalse(result)
            self.assertEqual(len(list((root / "backups").glob("scheduled-*.db"))), 1)
            self.assertTrue(runtime.snapshot().last_backup_at)
            self.assertGreater(
                (runtime._next_backup_due or 0) - runtime.monotonic(),
                86000,
            )
            self.assertTrue(any("备份轮换失败" in item for item in logger.warning_messages))

    def test_restart_does_not_repeat_a_recent_managed_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = EssenceRepository(
                root / "group_essence.db",
                backup_dir=root / "backups",
            )
            repository.init_db()
            repository.create_backup()
            runtime = GroupEssenceRuntime(
                service=FakeService(),  # type: ignore[arg-type]
                repository=repository,
                action_context=FakeGateway(),
                config=replace(
                    _runtime_config(),
                    scheduled_sync_enabled=False,
                    scheduled_sync_block_reason="disabled",
                    automatic_backups_enabled=True,
                    group_ids=(),
                    startup_delay_seconds=0.0,
                ),
                logger=FakeLogger(),
                health_path=root / "ge_health.json",
            )

            async def scenario() -> float:
                self.assertTrue(await runtime.start())
                remaining = (runtime._next_backup_due or 0) - runtime.monotonic()
                await runtime.stop()
                return remaining

            remaining = asyncio.run(scenario())

            self.assertGreater(remaining, 86000)
            self.assertEqual(len(list((root / "backups").glob("scheduled-*.db"))), 1)

    def test_supervisor_retries_after_unexpected_loop_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, _, _, logger = _make_runtime(
                root,
                config=replace(
                    _runtime_config(),
                    startup_delay_seconds=0.0,
                    retry_base_seconds=0.01,
                ),
            )
            calls = 0

            async def flaky_run(_: str) -> bool:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("unexpected secret")
                runtime._stop_event.set()
                return True

            runtime.run_group_once = flaky_run  # type: ignore[method-assign]

            async def scenario() -> None:
                self.assertTrue(await runtime.start())
                await asyncio.sleep(0.2)
                await runtime.stop()

            asyncio.run(scenario())

            self.assertGreaterEqual(calls, 2)
            self.assertTrue(any("继续重试" in item for item in logger.error_messages))
            self.assertTrue(
                all("unexpected secret" not in item for item in logger.error_messages)
            )


if __name__ == "__main__":
    unittest.main()

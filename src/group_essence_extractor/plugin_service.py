from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from .astrbot_source import (
    AstrBotEssenceSource,
    OneBotActionError,
    apply_history_sender_times,
)
from .db import EssenceRepository, SaveStats, SearchPage
from .models import EssenceMessage
from .normalization import needs_message_detail


VALIDATION_FIELDS = {
    "message_id": "message_id",
    "sender_time": "sender_time",
    "essence_time": "operator_time",
    "content": "content",
}


@dataclass(frozen=True)
class ValidationReport:
    collected: int
    by_content_type: dict[str, int]
    missing: dict[str, int]
    field_types: dict[str, str]
    detail_candidates: int
    detail_requested: int
    detail_skipped: int
    detail_failed: int


@dataclass(frozen=True)
class StatusReport:
    database_exists: bool
    total: int = 0
    schema_version: int | None = None
    missing_sender_time: int = 0


@dataclass(frozen=True)
class SyncReport:
    collected: int
    inserted: int
    updated: int
    refreshed: int
    unchanged: int
    sender_times_enriched: int = 0
    history_lookup_failed: bool = False


@dataclass(frozen=True)
class SenderTimeRepairReport:
    database_exists: bool
    history_scanned: int = 0
    candidates: int = 0
    matched: int = 0
    updated: int = 0
    remaining: int = 0


class PluginServiceError(RuntimeError):
    def __init__(self, public_message: str, category: str) -> None:
        self.public_message = public_message
        self.category = category
        super().__init__(public_message)


class GroupEssencePluginService:
    def __init__(
        self,
        source: AstrBotEssenceSource,
        repository: EssenceRepository,
        validation_detail_request_limit: int = 10,
        sync_detail_request_limit: int = 10,
        history_query_limit: int = 100,
    ) -> None:
        self.source = source
        self.repository = repository
        self.validation_detail_request_limit = max(
            0,
            min(int(validation_detail_request_limit), 50),
        )
        self.sync_detail_request_limit = max(
            0,
            min(int(sync_detail_request_limit), 50),
        )
        self.history_query_limit = max(0, min(int(history_query_limit), 500))
        self.operation_lock = asyncio.Lock()

    async def validate(self, event: Any, group_id: str) -> ValidationReport:
        async with self.operation_lock:
            messages = await self.source.get_essence_messages(
                event,
                group_id,
                detail_request_limit=self.validation_detail_request_limit,
            )
        return build_validation_report(messages)

    async def sync(self, event: Any, group_id: str) -> SyncReport:
        normalized_group_id = _require_group_id(group_id)
        async with self.operation_lock:
            previous_failures = await asyncio.to_thread(
                self.repository.previous_detail_failure_ids,
                normalized_group_id,
            )
            messages = await self.source.get_essence_messages(
                event,
                normalized_group_id,
                detail_request_limit=self.sync_detail_request_limit,
                skip_detail_ids=previous_failures,
            )
            unseen_ids = await asyncio.to_thread(
                self.repository.unseen_message_ids,
                normalized_group_id,
                messages,
            )
            sender_times_enriched = 0
            history_lookup_failed = False
            history_candidates = {
                message.message_id
                for message in messages
                if message.message_id in unseen_ids and not message.sender_time.strip()
            }
            if self.history_query_limit and history_candidates:
                try:
                    history = await self.source.get_group_history_times(
                        event,
                        normalized_group_id,
                        limit=self.history_query_limit,
                    )
                    messages, sender_times_enriched = apply_history_sender_times(
                        messages,
                        history,
                        candidate_message_ids=history_candidates,
                    )
                except OneBotActionError:
                    history_lookup_failed = True
            try:
                stats = await asyncio.to_thread(self._persist_messages, messages)
            except Exception as exc:
                raise PluginServiceError("数据库同步失败。", type(exc).__name__) from None
        return SyncReport(
            collected=len(messages),
            inserted=stats.inserted,
            updated=stats.updated,
            refreshed=stats.refreshed,
            unchanged=stats.unchanged,
            sender_times_enriched=sender_times_enriched,
            history_lookup_failed=history_lookup_failed,
        )

    async def repair_sender_times(
        self,
        event: Any,
        group_id: str,
        requested_limit: Any = None,
    ) -> SenderTimeRepairReport:
        normalized_group_id = _require_group_id(group_id)
        if not self.repository.db_path.is_file():
            return SenderTimeRepairReport(database_exists=False)
        if self.history_query_limit <= 0:
            raise PluginServiceError("群历史时间补全已关闭。", "history_disabled")
        limit = _clamp_history_limit(requested_limit, self.history_query_limit)
        async with self.operation_lock:
            history = await self.source.get_group_history_times(
                event,
                normalized_group_id,
                limit=limit,
            )
            try:
                stats = await asyncio.to_thread(
                    self.repository.backfill_sender_times,
                    normalized_group_id,
                    history,
                )
            except Exception as exc:
                raise PluginServiceError("发送时间补全失败。", type(exc).__name__) from None
        return SenderTimeRepairReport(
            database_exists=True,
            history_scanned=len(history),
            candidates=stats.candidates,
            matched=stats.matched,
            updated=stats.updated,
            remaining=stats.remaining,
        )

    async def search(self, group_id: str, keyword: str, limit: int) -> SearchPage:
        normalized_group_id = _require_group_id(group_id)
        normalized_keyword = str(keyword or "").strip()
        if not normalized_keyword:
            raise PluginServiceError("查询关键词不能为空。", "empty_keyword")
        return await self._search_page(
            group_id=normalized_group_id,
            content=normalized_keyword,
            limit=limit,
        )

    async def recent(self, group_id: str, limit: int) -> SearchPage:
        return await self._search_page(
            group_id=_require_group_id(group_id),
            content="",
            limit=limit,
        )

    async def status(self) -> StatusReport:
        if not self.repository.db_path.is_file():
            return StatusReport(database_exists=False)
        async with self.operation_lock:
            try:
                audit = await asyncio.to_thread(self.repository.audit)
            except Exception as exc:
                raise PluginServiceError("数据库状态读取失败。", type(exc).__name__) from None
        if audit.get("status") != "ok":
            raise PluginServiceError("数据库状态异常。", "audit_error")
        return StatusReport(
            database_exists=True,
            total=int(audit.get("total") or 0),
            schema_version=int(audit.get("schema_version") or 0),
            missing_sender_time=int(
                (audit.get("missing") or {}).get("sender_time") or 0
            ),
        )

    def _persist_messages(self, messages: list[EssenceMessage]) -> SaveStats:
        self.repository.init_db()
        return self.repository.upsert_messages(messages)

    async def _search_page(
        self,
        *,
        group_id: str,
        content: str,
        limit: int,
    ) -> SearchPage:
        normalized_limit = _clamp_limit(limit)
        if not self.repository.db_path.is_file():
            return SearchPage(items=[], total=0, limit=normalized_limit, offset=0)
        async with self.operation_lock:
            try:
                return await asyncio.to_thread(
                    self.repository.search_page,
                    group_id=group_id,
                    content=content,
                    limit=normalized_limit,
                    offset=0,
                )
            except Exception as exc:
                raise PluginServiceError("数据库查询失败。", type(exc).__name__) from None


def build_validation_report(messages: list[EssenceMessage]) -> ValidationReport:
    missing = {
        "message_id": sum(not message.message_id.strip() for message in messages),
        "sender_time": sum(not message.sender_time.strip() for message in messages),
        "essence_time": sum(not message.essence_time.strip() for message in messages),
        "content": sum(
            not message.content_text.strip() or message.content_text == "[空消息]"
            for message in messages
        ),
    }
    raw_items = [
        raw_item
        for message in messages
        if isinstance(raw_item := (message.raw_data or {}).get("essence"), Mapping)
    ]
    field_types = {
        report_name: _type_summary(item.get(source_name) for item in raw_items)
        for report_name, source_name in VALIDATION_FIELDS.items()
    }
    detail_candidates = sum(needs_message_detail(item) for item in raw_items)
    detail_requested = sum(
        bool((message.raw_data or {}).get("message_detail_requested"))
        for message in messages
    )
    return ValidationReport(
        collected=len(messages),
        by_content_type=dict(
            sorted(Counter(message.content_type for message in messages).items())
        ),
        missing=missing,
        field_types=field_types,
        detail_candidates=detail_candidates,
        detail_requested=detail_requested,
        detail_skipped=max(0, detail_candidates - detail_requested),
        detail_failed=sum(
            bool((message.raw_data or {}).get("message_detail_error"))
            for message in messages
        ),
    )


def format_validation_report(report: ValidationReport) -> str:
    content_types = _format_counts(report.by_content_type)
    missing = _format_counts(report.missing)
    field_types = ", ".join(
        f"{name}={value}" for name, value in report.field_types.items()
    )
    return "\n".join(
        (
            "精华验收成功",
            "目标群：已授权",
            f"采集数量：{report.collected}",
            f"内容类型：{content_types}",
            f"缺失字段：{missing}",
            f"字段类型：{field_types}",
            "详情补全："
            f"候选={report.detail_candidates}, 请求={report.detail_requested}, "
            f"跳过={report.detail_skipped}, 失败={report.detail_failed}",
        )
    )


def format_status_report(
    report: StatusReport,
    *,
    validation_mode: bool,
    allowed_group_count: int,
) -> str:
    mode = "只读验收" if validation_mode else "同步与查询"
    database = "未初始化" if not report.database_exists else "可用"
    lines = [
        "精华状态",
        f"运行模式：{mode}",
        f"授权群数量：{allowed_group_count}",
        f"数据库：{database}",
    ]
    if report.database_exists:
        lines.extend(
            (
                f"记录数量：{report.total}",
                f"数据库版本：{report.schema_version}",
                f"发送时间缺失：{report.missing_sender_time}",
            )
        )
    return "\n".join(lines)


def format_sync_report(report: SyncReport) -> str:
    return "\n".join(
        (
            "精华同步完成",
            "目标群：已授权",
            f"采集数量：{report.collected}",
            f"新增：{report.inserted}",
            f"更新：{report.updated}",
            f"元数据刷新：{report.refreshed}",
            f"未变化：{report.unchanged}",
            f"新记录时间补全：{report.sender_times_enriched}",
            f"历史查询失败：{'是' if report.history_lookup_failed else '否'}",
        )
    )


def format_sender_time_repair_report(report: SenderTimeRepairReport) -> str:
    if not report.database_exists:
        return "发送时间补全\n数据库尚未初始化，请先执行精华同步。"
    return "\n".join(
        (
            "发送时间补全完成",
            "目标群：已授权",
            f"历史元数据：{report.history_scanned}",
            f"待补记录：{report.candidates}",
            f"匹配：{report.matched}",
            f"更新：{report.updated}",
            f"仍缺失：{report.remaining}",
        )
    )


def format_search_page(
    page: SearchPage,
    *,
    max_content_chars: int,
    title: str = "精华查询结果",
) -> str:
    if not page.items:
        return f"{title}\n未找到匹配记录。"

    lines = [f"{title}：{len(page.items)}/{page.total}"]
    for index, item in enumerate(page.items, start=1):
        timestamp = _safe_reply_text(
            item.get("essence_time") or item.get("sender_time") or "未知时间",
            32,
        )
        sender = _safe_reply_text(item.get("sender") or "未知发送者", 80)
        content = _safe_reply_text(
            item.get("content_text") or "[空消息]",
            _clamp_content_limit(max_content_chars),
        )
        content_type = _safe_reply_text(item.get("content_type") or "unknown", 32)
        lines.extend(
            (
                f"[{index}/{len(page.items)}] {timestamp}",
                f"发送者：{sender}",
                f"内容：{content}",
                f"类型：{content_type}",
            )
        )
    remaining = max(0, page.total - len(page.items))
    if remaining:
        lines.append(f"另有 {remaining} 条结果未显示。")
    return "\n".join(lines)


def _type_summary(values: Any) -> str:
    names = sorted({type(value).__name__ for value in values})
    return "|".join(names) if names else "missing"


def _format_counts(values: Mapping[str, int]) -> str:
    return ", ".join(f"{name}={count}" for name, count in values.items()) or "无"


def _require_group_id(group_id: str) -> str:
    normalized = str(group_id or "").strip()
    if not normalized:
        raise PluginServiceError("目标群无效。", "missing_group_id")
    return normalized


def _safe_reply_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"https?://\S+", "[链接已隐藏]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(?:token|cookie|authorization)\s*[:=]\s*\S+",
        "[凭据已隐藏]",
        text,
    )
    text = re.sub(r"(?i)(?<!\w)[a-z]:[\\/][^\s]+", "[路径已隐藏]", text)
    text = re.sub(r"(?<!\w)/(?:[^/\s]+/)+[^\s]+", "[路径已隐藏]", text)
    if len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _clamp_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 5
    return max(1, min(parsed, 20))


def _clamp_content_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 300
    return max(50, min(parsed, 2000))


def _clamp_history_limit(value: Any, maximum: int) -> int:
    if value is None or not str(value).strip():
        return maximum
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise PluginServiceError("历史数量必须是正整数。", "invalid_history_limit") from None
    if parsed < 1 or parsed > maximum:
        raise PluginServiceError(
            f"历史数量必须在 1 到 {maximum} 之间。",
            "invalid_history_limit",
        )
    return parsed

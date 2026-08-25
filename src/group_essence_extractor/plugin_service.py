from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .astrbot_source import AstrBotEssenceSource
from .db import EssenceRepository
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
    detail_requested: int
    detail_failed: int


@dataclass(frozen=True)
class StatusReport:
    database_exists: bool
    total: int = 0
    schema_version: int | None = None


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
    ) -> None:
        self.source = source
        self.repository = repository
        self.operation_lock = asyncio.Lock()

    async def validate(self, event: Any, group_id: str) -> ValidationReport:
        async with self.operation_lock:
            messages = await self.source.get_essence_messages(event, group_id)
        return build_validation_report(messages)

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
        )


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
    return ValidationReport(
        collected=len(messages),
        by_content_type=dict(
            sorted(Counter(message.content_type for message in messages).items())
        ),
        missing=missing,
        field_types=field_types,
        detail_requested=sum(needs_message_detail(item) for item in raw_items),
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
            f"详情补全：请求={report.detail_requested}, 失败={report.detail_failed}",
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
            )
        )
    return "\n".join(lines)


def _type_summary(values: Any) -> str:
    names = sorted({type(value).__name__ for value in values})
    return "|".join(names) if names else "missing"


def _format_counts(values: Mapping[str, int]) -> str:
    return ", ".join(f"{name}={count}" for name, count in values.items()) or "无"

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


DEFAULT_ADMIN_IDS = ("2573423682",)


class AuthorizationEventLike(Protocol):
    def get_sender_id(self) -> str: ...

    def get_group_id(self) -> str | None: ...


@dataclass(frozen=True)
class PluginSettings:
    validation_mode: bool
    admin_ids: frozenset[str]
    allowed_group_ids: frozenset[str]
    default_group_id: str
    max_validation_detail_requests: int
    max_sync_detail_requests: int
    history_query_limit: int
    max_query_results: int
    max_content_chars: int
    max_reply_images: int
    enable_image_enrichment: bool
    enable_scheduled_sync: bool
    onebot_platform_id: str
    scheduled_sync_interval_minutes: int
    scheduled_sync_startup_delay_seconds: int
    scheduled_sync_timeout_seconds: int
    scheduled_sync_jitter_percent: int
    scheduled_sync_failure_threshold: int
    scheduled_sync_retry_base_seconds: int
    scheduled_sync_max_backoff_minutes: int
    detail_retry_base_minutes: int
    detail_retry_max_hours: int
    enable_failure_alerts: bool
    enable_automatic_backups: bool
    backup_interval_hours: int
    backup_keep_daily: int
    backup_keep_weekly: int

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any] | None) -> PluginSettings:
        values = config or {}
        return cls(
            validation_mode=_as_bool(values.get("validation_mode"), True),
            admin_ids=frozenset(
                _string_list(values.get("admin_ids"), default=DEFAULT_ADMIN_IDS)
            ),
            allowed_group_ids=frozenset(_string_list(values.get("allowed_group_ids"))),
            default_group_id=str(values.get("default_group_id") or "").strip(),
            max_validation_detail_requests=_clamp_int(
                values.get("max_validation_detail_requests"), 10, 0, 50
            ),
            max_sync_detail_requests=_clamp_int(
                values.get("max_sync_detail_requests"), 10, 0, 50
            ),
            history_query_limit=_clamp_int(
                values.get("history_query_limit"), 100, 0, 500
            ),
            max_query_results=_clamp_int(values.get("max_query_results"), 5, 1, 20),
            max_content_chars=_clamp_int(values.get("max_content_chars"), 300, 50, 2000),
            max_reply_images=_clamp_int(values.get("max_reply_images"), 5, 0, 10),
            enable_image_enrichment=_as_bool(
                values.get("enable_image_enrichment"), False
            ),
            enable_scheduled_sync=_as_bool(values.get("enable_scheduled_sync"), False),
            onebot_platform_id=str(values.get("onebot_platform_id") or "").strip(),
            scheduled_sync_interval_minutes=_clamp_int(
                values.get("scheduled_sync_interval_minutes"), 30, 5, 1440
            ),
            scheduled_sync_startup_delay_seconds=_clamp_int(
                values.get("scheduled_sync_startup_delay_seconds"), 60, 0, 3600
            ),
            scheduled_sync_timeout_seconds=_clamp_int(
                values.get("scheduled_sync_timeout_seconds"), 90, 10, 600
            ),
            scheduled_sync_jitter_percent=_clamp_int(
                values.get("scheduled_sync_jitter_percent"), 10, 0, 30
            ),
            scheduled_sync_failure_threshold=_clamp_int(
                values.get("scheduled_sync_failure_threshold"), 3, 1, 10
            ),
            scheduled_sync_retry_base_seconds=_clamp_int(
                values.get("scheduled_sync_retry_base_seconds"), 30, 5, 3600
            ),
            scheduled_sync_max_backoff_minutes=_clamp_int(
                values.get("scheduled_sync_max_backoff_minutes"), 60, 1, 1440
            ),
            detail_retry_base_minutes=_clamp_int(
                values.get("detail_retry_base_minutes"), 15, 1, 1440
            ),
            detail_retry_max_hours=_clamp_int(
                values.get("detail_retry_max_hours"), 24, 1, 168
            ),
            enable_failure_alerts=_as_bool(
                values.get("enable_failure_alerts"), True
            ),
            enable_automatic_backups=_as_bool(
                values.get("enable_automatic_backups"), False
            ),
            backup_interval_hours=_clamp_int(
                values.get("backup_interval_hours"), 24, 1, 168
            ),
            backup_keep_daily=_clamp_int(
                values.get("backup_keep_daily"), 7, 1, 31
            ),
            backup_keep_weekly=_clamp_int(
                values.get("backup_keep_weekly"), 4, 0, 52
            ),
        )

    @property
    def scheduled_sync_block_reason(self) -> str:
        if not self.enable_scheduled_sync:
            return "disabled"
        if self.validation_mode:
            return "validation_mode"
        if not self.allowed_group_ids:
            return "missing_allowed_groups"
        if not self.onebot_platform_id:
            return "missing_platform_id"
        return ""

    @property
    def background_work_enabled(self) -> bool:
        if self.validation_mode:
            return False
        return (
            not self.scheduled_sync_block_reason
            or self.enable_automatic_backups
        )

    def is_admin(self, event: AuthorizationEventLike) -> bool:
        return str(event.get_sender_id() or "").strip() in self.admin_ids

    def resolve_authorized_group(
        self,
        event: AuthorizationEventLike,
        explicit_group_id: str = "",
    ) -> str | None:
        if not self.is_admin(event):
            return None
        target = str(explicit_group_id or "").strip()
        if not target:
            target = str(event.get_group_id() or self.default_group_id or "").strip()
        return target if target and target in self.allowed_group_ids else None


def _string_list(value: Any, default: tuple[str, ...] = ()) -> list[str]:
    source = value if isinstance(value, (list, tuple, set, frozenset)) else default
    return [text for item in source if (text := str(item or "").strip())]


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))

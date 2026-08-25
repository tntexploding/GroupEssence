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
    max_query_results: int
    max_content_chars: int
    enable_image_enrichment: bool
    enable_scheduled_sync: bool

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
            max_query_results=_clamp_int(values.get("max_query_results"), 5, 1, 20),
            max_content_chars=_clamp_int(values.get("max_content_chars"), 300, 50, 2000),
            enable_image_enrichment=_as_bool(
                values.get("enable_image_enrichment"), False
            ),
            enable_scheduled_sync=_as_bool(values.get("enable_scheduled_sync"), False),
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

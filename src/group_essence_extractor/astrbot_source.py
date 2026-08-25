from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
import re
from typing import Any, Protocol

from .models import EssenceMessage
from .normalization import (
    get_message_id,
    needs_message_detail,
    normalize_essence_items,
)


ActionCaller = Callable[..., Awaitable[Any]]


class AstrBotEventLike(Protocol):
    bot: Any


class OneBotActionError(RuntimeError):
    def __init__(
        self,
        action: str,
        *,
        status: Any = "",
        retcode: Any = None,
        wording: Any = "",
    ) -> None:
        self.action = _safe_action(action)
        self.status = _safe_fragment(status, 32) or "n/a"
        self.retcode = _safe_fragment(retcode, 32) or "n/a"
        self.wording = _safe_fragment(wording, 120)
        super().__init__(self.public_message)

    @classmethod
    def from_envelope(cls, action: str, envelope: Mapping[str, Any]) -> OneBotActionError:
        return cls(
            action,
            status=envelope.get("status"),
            retcode=envelope.get("retcode"),
            wording=(
                envelope.get("wording")
                or envelope.get("msg")
                or envelope.get("message")
                or ""
            ),
        )

    @property
    def public_message(self) -> str:
        return (
            f"OneBot action={self.action}, status={self.status}, "
            f"retcode={self.retcode}"
        )


def unwrap_action_result(result: Any, *, action: str = "unknown") -> Any:
    if isinstance(result, Mapping) and "data" in result:
        status = str(result.get("status", "")).lower()
        retcode = result.get("retcode")
        if (status and status != "ok") or retcode not in (None, 0):
            raise OneBotActionError.from_envelope(action, result)
        return result["data"]
    return result


class AstrBotEssenceSource:
    async def get_essence_messages(
        self,
        event: AstrBotEventLike,
        group_id: str,
    ) -> list[EssenceMessage]:
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            raise ValueError("调用 OneBot Action 时必须提供目标群")

        call_action = _resolve_action_caller(event)
        group_parameter: str | int = (
            int(normalized_group_id)
            if normalized_group_id.isdigit()
            else normalized_group_id
        )
        items = await _call_action(
            call_action,
            "get_essence_msg_list",
            group_id=group_parameter,
        )
        if not isinstance(items, list):
            raise OneBotActionError(
                "get_essence_msg_list",
                status="invalid_data",
                wording="data is not a list",
            )

        valid_items: list[Mapping[str, Any]] = []
        details: dict[str, Mapping[str, Any]] = {}
        detail_errors: dict[str, str] = {}
        for item in items:
            if not isinstance(item, Mapping):
                continue
            valid_items.append(item)
            if not needs_message_detail(item):
                continue

            message_id = get_message_id(item)
            if not message_id:
                continue
            try:
                detail = await _call_action(
                    call_action,
                    "get_msg",
                    message_id=message_id,
                )
                if isinstance(detail, Mapping):
                    details[message_id] = detail
            except OneBotActionError as exc:
                detail_errors[message_id] = exc.public_message

        return normalize_essence_items(
            valid_items,
            requested_group_id=normalized_group_id,
            details=details,
            detail_errors=detail_errors,
        )


def _resolve_action_caller(event: AstrBotEventLike) -> ActionCaller:
    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    if api is None:
        api = getattr(bot, "_api", None)
    call_action = getattr(api, "call_action", None)
    if not callable(call_action):
        raise OneBotActionError("resolve_api", status="unavailable")
    return call_action


async def _call_action(call_action: ActionCaller, action: str, **params: Any) -> Any:
    try:
        result = await call_action(action=action, **params)
        return unwrap_action_result(result, action=action)
    except OneBotActionError:
        raise
    except Exception as exc:
        raise OneBotActionError(
            action,
            status="exception",
            wording=type(exc).__name__,
        ) from None


def _safe_action(value: Any) -> str:
    action = re.sub(r"[^a-zA-Z0-9_.-]", "", str(value or ""))
    return action[:64] or "unknown"


def _safe_fragment(value: Any, limit: int) -> str:
    text = " ".join(str(value if value is not None else "").split())
    text = re.sub(r"https?://\S+", "[redacted]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(?:token|cookie|authorization)\s*[:=]\s*\S+",
        "[redacted]",
        text,
    )
    text = re.sub(r"(?<!\d)\d{5,}(?!\d)", "[id]", text)
    return text[:limit]

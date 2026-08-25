from __future__ import annotations

from typing import Any, Protocol

from .astrbot_source import OneBotActionError, unwrap_action_result


class AstrBotContextLike(Protocol):
    def get_platform_inst(self, platform_id: str) -> Any: ...


class AstrBotOneBotGateway:
    """Resolve AstrBot's configured AIOCQHTTP client without retaining an event."""

    def __init__(self, context: AstrBotContextLike, platform_id: str) -> None:
        self.context = context
        self.platform_id = str(platform_id or "").strip()

    async def call_action(self, *, action: str, **params: Any) -> Any:
        try:
            client = self._resolve_client()
            return await client.call_action(action=action, **params)
        except OneBotActionError:
            raise
        except Exception as exc:
            raise OneBotActionError(
                action,
                status="exception",
                wording=type(exc).__name__,
            ) from None

    async def send_private_text(self, user_id: str, message: str) -> None:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise OneBotActionError("send_private_msg", status="missing_user_id")
        target: str | int = (
            int(normalized_user_id)
            if normalized_user_id.isdigit()
            else normalized_user_id
        )
        result = await self.call_action(
            action="send_private_msg",
            user_id=target,
            message=str(message or ""),
        )
        unwrap_action_result(result, action="send_private_msg")

    def _resolve_client(self) -> Any:
        if not self.platform_id:
            raise OneBotActionError("resolve_platform", status="missing_id")
        get_platform_inst = getattr(self.context, "get_platform_inst", None)
        if not callable(get_platform_inst):
            raise OneBotActionError("resolve_platform", status="unsupported_context")
        try:
            platform = get_platform_inst(self.platform_id)
        except Exception as exc:
            raise OneBotActionError(
                "resolve_platform",
                status="exception",
                wording=type(exc).__name__,
            ) from None
        if platform is None:
            raise OneBotActionError("resolve_platform", status="not_found")

        meta_method = getattr(platform, "meta", None)
        metadata = meta_method() if callable(meta_method) else None
        adapter_name = str(getattr(metadata, "name", "") or "").strip().lower()
        if adapter_name != "aiocqhttp":
            raise OneBotActionError("resolve_platform", status="wrong_adapter")

        get_client = getattr(platform, "get_client", None)
        if not callable(get_client):
            raise OneBotActionError("resolve_client", status="unavailable")
        client = get_client()
        if not callable(getattr(client, "call_action", None)):
            raise OneBotActionError("resolve_client", status="invalid_client")
        return client

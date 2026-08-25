from __future__ import annotations

from typing import Any

import requests

from .models import EssenceMessage
from .normalization import (
    get_message_id,
    needs_message_detail,
    normalize_essence_items,
)


class OneBotClient:
    def __init__(
        self,
        base_url: str,
        access_token: str = "",
        timeout_seconds: float = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.headers = {}
        if access_token:
            self.headers["Authorization"] = f"Bearer {access_token}"

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> OneBotClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_essence_messages(self, group_id: str = "") -> list[EssenceMessage]:
        group_id = group_id.strip()
        if not group_id:
            raise ValueError("调用 OneBot 获取群精华消息时必须配置 GROUP_ID")

        items = self._post_action("get_essence_msg_list", {"group_id": group_id})
        if items is None:
            return []
        if not isinstance(items, list):
            raise RuntimeError("OneBot 精华消息接口返回的 data 不是列表")

        valid_items: list[dict[str, Any]] = []
        details: dict[str, dict[str, Any]] = {}
        detail_errors: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            valid_items.append(item)
            if not needs_message_detail(item):
                continue

            message_id = get_message_id(item)
            if not message_id:
                continue
            try:
                detail_data = self._post_action("get_msg", {"message_id": message_id})
                if isinstance(detail_data, dict):
                    details[message_id] = detail_data
            except Exception as exc:
                # 精华列表仍可用时，不应因单条详情补全失败而丢弃整批数据。
                detail_errors[message_id] = str(exc)

        return normalize_essence_items(
            valid_items,
            requested_group_id=group_id,
            details=details,
            detail_errors=detail_errors,
        )

    def _post_action(self, action: str, payload: dict[str, Any]) -> Any:
        response = self.session.post(
            f"{self.base_url}/{action}",
            json=payload,
            headers=self.headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        envelope = response.json()
        _raise_if_onebot_failed(envelope)
        return envelope.get("data")


def _raise_if_onebot_failed(envelope: Any) -> None:
    if not isinstance(envelope, dict):
        raise RuntimeError(f"OneBot 返回非 JSON 对象: {envelope}")

    # NapCat / OneBot v11 通常会返回 status/retcode/wording。
    status = str(envelope.get("status", "")).lower()
    retcode = envelope.get("retcode")
    if (status and status != "ok") or (retcode not in (None, 0)):
        wording = (
            envelope.get("wording")
            or envelope.get("msg")
            or envelope.get("message")
            or "unknown error"
        )
        raise RuntimeError(
            f"OneBot 接口失败: status={status or 'n/a'}, "
            f"retcode={retcode}, msg={wording}"
        )

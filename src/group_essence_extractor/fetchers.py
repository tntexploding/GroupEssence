from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from .models import EssenceMessage


class OneBotClient:
    def __init__(self, base_url: str, access_token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {}
        if access_token:
            self.headers["Authorization"] = f"Bearer {access_token}"

    def get_essence_messages(self, group_id: str = "") -> list[EssenceMessage]:
        payload: dict[str, Any] = {}
        if group_id:
            payload["group_id"] = int(group_id)

        resp = requests.post(
            f"{self.base_url}/get_essence_msg_list",
            json=payload,
            headers=self.headers,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        _raise_if_onebot_failed(data)

        items = data.get("data", []) if isinstance(data, dict) else []
        messages: list[EssenceMessage] = []
        for item in items:
            sender_time = _fmt_ts(item.get("sender_time"))
            essence_time = _fmt_ts(item.get("operator_time"))
            content_text, content_type, image_path, ocr_text = _parse_message_content(item)
            sender_id = _pick_id(item, "sender_uin", "sender_id")
            operator_id = _pick_id(item, "operator_uin", "operator_id")

            messages.append(
                EssenceMessage(
                    sender=str(item.get("sender_nick") or sender_id or "未知发送者"),
                    sender_id=sender_id,
                    sender_time=sender_time,
                    essence_time=essence_time,
                    operator=str(item.get("operator_nick") or operator_id or "未知设置人"),
                    operator_id=operator_id,
                    content_text=content_text,
                    content_type=content_type,
                    image_path=image_path,
                    ocr_text=ocr_text,
                    group_id=str(item.get("group_id") or ""),
                    message_id=str(item.get("message_id") or ""),
                    source="onebot",
                    raw_data=item,
                )
            )
        return messages


def _fmt_ts(ts: Any) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(ts)


def _parse_message_content(item: dict[str, Any]) -> tuple[str, str, str, str]:
    segments = item.get("content")
    if not isinstance(segments, list):
        text = str(segments) if segments is not None else ""
        return text, "text", "", ""

    text_parts: list[str] = []
    image_urls: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            text_parts.append(str(seg))
            continue
        seg_type = seg.get("type")
        seg_data = seg.get("data", {})
        if seg_type == "text":
            text_parts.append(str(seg_data.get("text", "")))
        elif seg_type == "image":
            image_urls.append(str(seg_data.get("url") or seg_data.get("file") or ""))

    if image_urls and not text_parts:
        return "[图片消息]", "image", image_urls[0], ""
    return "".join(text_parts).strip() or "[空消息]", "text", "", ""


def _raise_if_onebot_failed(data: Any) -> None:
    if not isinstance(data, dict):
        raise RuntimeError(f"OneBot 返回非 JSON 对象: {data}")

    # NapCat / OneBot v11 通常会返回 status/retcode/wording。
    status = str(data.get("status", "")).lower()
    retcode = data.get("retcode")
    if (status and status != "ok") or (retcode not in (None, 0)):
        wording = data.get("wording") or data.get("msg") or data.get("message") or "unknown error"
        raise RuntimeError(f"OneBot 接口失败: status={status or 'n/a'}, retcode={retcode}, msg={wording}")


def _pick_id(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""

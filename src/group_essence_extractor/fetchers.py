from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import requests

from .models import EssenceMessage


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

        messages: list[EssenceMessage] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            detail: dict[str, Any] = {}
            detail_error = ""
            if _needs_message_detail(item):
                message_id = _pick_id(item, "message_id")
                if message_id:
                    try:
                        detail_data = self._post_action("get_msg", {"message_id": message_id})
                        if isinstance(detail_data, dict):
                            detail = detail_data
                    except Exception as exc:
                        # 精华列表本身仍然可用时，不应因单条消息补全失败而丢弃整批数据。
                        detail_error = str(exc)

            sender_info = detail.get("sender")
            if not isinstance(sender_info, dict):
                sender_info = {}

            sender_time = _fmt_ts(_first_value(item.get("sender_time"), detail.get("time")))
            essence_time = _fmt_ts(item.get("operator_time"))
            content_source = _first_value(
                item.get("content"),
                detail.get("message"),
                detail.get("content"),
                detail.get("raw_message"),
            )
            content_text, content_type, image_path, ocr_text = _parse_message_content(
                {"content": content_source}
            )
            sender_id = _pick_id(item, "sender_id", "sender_uin") or _pick_id(
                detail, "user_id", "sender_id"
            ) or _pick_id(sender_info, "user_id", "sender_id")
            operator_id = _pick_id(item, "operator_id", "operator_uin")

            raw_data: dict[str, Any] = {"essence": item}
            if detail:
                raw_data["message_detail"] = detail
            if detail_error:
                raw_data["message_detail_error"] = detail_error

            messages.append(
                EssenceMessage(
                    sender=str(
                        item.get("sender_nick")
                        or sender_info.get("card")
                        or sender_info.get("nickname")
                        or sender_id
                        or "未知发送者"
                    ),
                    sender_id=sender_id,
                    sender_time=sender_time,
                    essence_time=essence_time,
                    operator=str(item.get("operator_nick") or operator_id or "未知设置人"),
                    operator_id=operator_id,
                    content_text=content_text,
                    content_type=content_type,
                    image_path=image_path,
                    ocr_text=ocr_text,
                    group_id=str(item.get("group_id") or detail.get("group_id") or group_id),
                    message_id=str(item.get("message_id") or ""),
                    source="onebot",
                    raw_data=raw_data,
                )
            )
        return messages

    def _post_action(self, action: str, payload: dict[str, Any]) -> Any:
        resp = self.session.post(
            f"{self.base_url}/{action}",
            json=payload,
            headers=self.headers,
            timeout=self.timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        _raise_if_onebot_failed(data)
        return data.get("data")


def _fmt_ts(ts: Any) -> str:
    if ts is None or (isinstance(ts, str) and not ts.strip()):
        return ""

    original = str(ts).strip()
    try:
        value = float(original)
        # 同时兼容秒、毫秒、微秒和纳秒时间戳。
        while abs(value) > 253_402_300_799:
            value /= 1000
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return original


def _parse_message_content(item: dict[str, Any]) -> tuple[str, str, str, str]:
    segments = _first_value(item.get("content"), item.get("message"))
    if isinstance(segments, Mapping):
        segments = [segments]
    if not isinstance(segments, list):
        text = str(segments) if segments is not None else ""
        return text.strip() or "[空消息]", "text", "", ""

    text_parts: list[str] = []
    image_urls: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            text_parts.append(str(seg))
            continue
        seg_type = seg.get("type")
        seg_data = seg.get("data", {})
        if not isinstance(seg_data, Mapping):
            seg_data = {}
        if seg_type == "text":
            text_parts.append(str(seg_data.get("text", "")))
        elif seg_type == "image":
            image_url = str(seg_data.get("url") or seg_data.get("file") or "").strip()
            if image_url and image_url not in image_urls:
                image_urls.append(image_url)
        elif seg_type == "at":
            target = str(seg_data.get("name") or seg_data.get("qq") or "").strip()
            text_parts.append(f"@{target}" if target else "[@消息]")
        elif seg_type == "reply":
            text_parts.append("[回复消息]")
        elif seg_type == "face":
            face_id = str(seg_data.get("id") or "").strip()
            text_parts.append(f"[表情:{face_id}]" if face_id else "[表情]")
        elif seg_type:
            text_parts.append(f"[{seg_type}]")

    text = "".join(text_parts).strip()
    image_path = "\n".join(image_urls)
    if image_urls:
        content_type = "mixed" if text else "image"
        return text or "[图片消息]", content_type, image_path, ""
    return text or "[空消息]", "text", "", ""


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


def _first_value(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        return value
    return None


def _needs_message_detail(item: dict[str, Any]) -> bool:
    return _first_value(item.get("sender_time")) is None or _first_value(item.get("content")) is None

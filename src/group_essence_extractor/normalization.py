from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from .models import EssenceMessage


def format_timestamp(timestamp: Any) -> str:
    if timestamp is None or (isinstance(timestamp, str) and not timestamp.strip()):
        return ""

    original = str(timestamp).strip()
    try:
        value = float(original)
        # 同时兼容秒、毫秒、微秒和纳秒时间戳。
        while abs(value) > 253_402_300_799:
            value /= 1000
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return original


def parse_message_content(item: Mapping[str, Any]) -> tuple[str, str, str, str]:
    segments = first_value(item.get("content"), item.get("message"))
    if isinstance(segments, Mapping):
        segments = [segments]
    if not isinstance(segments, list):
        text = str(segments) if segments is not None else ""
        return text.strip() or "[空消息]", "text", "", ""

    text_parts: list[str] = []
    image_urls: list[str] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            text_parts.append(str(segment))
            continue
        segment_type = segment.get("type")
        segment_data = segment.get("data", {})
        if not isinstance(segment_data, Mapping):
            segment_data = {}
        if segment_type == "text":
            text_parts.append(str(segment_data.get("text", "")))
        elif segment_type == "image":
            image_url = str(
                segment_data.get("url") or segment_data.get("file") or ""
            ).strip()
            if image_url and image_url not in image_urls:
                image_urls.append(image_url)
        elif segment_type == "at":
            target = str(segment_data.get("name") or segment_data.get("qq") or "").strip()
            text_parts.append(f"@{target}" if target else "[@消息]")
        elif segment_type == "reply":
            text_parts.append("[回复消息]")
        elif segment_type == "face":
            face_id = str(segment_data.get("id") or "").strip()
            text_parts.append(f"[表情:{face_id}]" if face_id else "[表情]")
        elif segment_type:
            text_parts.append(f"[{segment_type}]")

    text = "".join(text_parts).strip()
    image_path = "\n".join(image_urls)
    if image_urls:
        content_type = "mixed" if text else "image"
        return text or "[图片消息]", content_type, image_path, ""
    return text or "[空消息]", "text", "", ""


def pick_id(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def first_value(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        return value
    return None


def needs_message_detail(item: Mapping[str, Any]) -> bool:
    return first_value(item.get("sender_time")) is None or first_value(
        item.get("content")
    ) is None


def get_message_id(item: Mapping[str, Any]) -> str:
    return pick_id(item, "message_id")


def normalize_essence_item(
    item: Mapping[str, Any],
    *,
    requested_group_id: str,
    detail: Mapping[str, Any] | None = None,
    detail_error: str = "",
) -> EssenceMessage:
    detail_data = detail or {}
    sender_info = detail_data.get("sender")
    if not isinstance(sender_info, Mapping):
        sender_info = {}

    sender_time = format_timestamp(
        first_value(item.get("sender_time"), detail_data.get("time"))
    )
    essence_time = format_timestamp(item.get("operator_time"))
    content_source = first_value(
        item.get("content"),
        detail_data.get("message"),
        detail_data.get("content"),
        detail_data.get("raw_message"),
    )
    content_text, content_type, image_path, ocr_text = parse_message_content(
        {"content": content_source}
    )
    sender_id = pick_id(item, "sender_id", "sender_uin") or pick_id(
        detail_data, "user_id", "sender_id"
    ) or pick_id(sender_info, "user_id", "sender_id")
    operator_id = pick_id(item, "operator_id", "operator_uin")

    raw_data: dict[str, Any] = {"essence": dict(item)}
    if detail_data:
        raw_data["message_detail"] = dict(detail_data)
    if detail_error:
        raw_data["message_detail_error"] = detail_error

    return EssenceMessage(
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
        group_id=str(
            first_value(
                item.get("group_id"),
                detail_data.get("group_id"),
                requested_group_id,
            )
            or ""
        ),
        message_id=get_message_id(item),
        source="onebot",
        raw_data=raw_data,
    )


def normalize_essence_items(
    items: Iterable[Mapping[str, Any]],
    *,
    requested_group_id: str,
    details: Mapping[str, Mapping[str, Any]] | None = None,
    detail_errors: Mapping[str, str] | None = None,
) -> list[EssenceMessage]:
    detail_by_id = {str(key): value for key, value in (details or {}).items()}
    error_by_id = {str(key): value for key, value in (detail_errors or {}).items()}
    return [
        normalize_essence_item(
            item,
            requested_group_id=requested_group_id,
            detail=detail_by_id.get(get_message_id(item)),
            detail_error=error_by_id.get(get_message_id(item), ""),
        )
        for item in items
    ]

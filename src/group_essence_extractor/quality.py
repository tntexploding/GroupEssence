from __future__ import annotations

from collections import Counter
from typing import Any

from .models import EssenceMessage


def summarize_messages(messages: list[EssenceMessage]) -> dict[str, Any]:
    missing_fields = {
        field: sum(field_is_missing(message, field) for message in messages)
        for field in (
            "group_id",
            "message_id",
            "sender",
            "sender_id",
            "sender_time",
            "essence_time",
            "operator",
            "operator_id",
            "content_text",
        )
    }
    ocr_messages = [message for message in messages if message.source == "ocr_screenshot"]
    ocr_confidences = [
        confidence
        for message in ocr_messages
        if isinstance(
            confidence := (message.raw_data or {}).get("ocr_mean_confidence"),
            (int, float),
        )
    ]
    structured_fields = (
        "sender",
        "sender_time",
        "essence_time",
        "operator",
        "content_text",
    )
    return {
        "total": len(messages),
        "by_source": dict(sorted(Counter(message.source for message in messages).items())),
        "by_content_type": dict(
            sorted(Counter(message.content_type for message in messages).items())
        ),
        "missing": missing_fields,
        "detail_errors": sum(
            bool((message.raw_data or {}).get("message_detail_error")) for message in messages
        ),
        "images_without_ocr": sum(
            bool(message.image_path) and not bool(message.ocr_text) for message in messages
        ),
        "ocr_quality": {
            "records": len(ocr_messages),
            "structured_complete": sum(
                not any(field_is_missing(message, field) for field in structured_fields)
                for message in ocr_messages
            ),
            "mean_confidence": (
                round(sum(ocr_confidences) / len(ocr_confidences), 2)
                if ocr_confidences
                else None
            ),
            "by_parser_profile": dict(
                sorted(
                    Counter(
                        str((message.raw_data or {}).get("parser_profile") or "unknown")
                        for message in ocr_messages
                    ).items()
                )
            ),
            "by_recognition_profile": dict(
                sorted(
                    Counter(
                        str((message.raw_data or {}).get("ocr_profile") or "unknown")
                        for message in ocr_messages
                    ).items()
                )
            ),
        },
    }


def field_is_missing(message: EssenceMessage, field: str) -> bool:
    value = str(getattr(message, field, "") or "").strip()
    if not value:
        return True
    return (field == "sender" and value == "未知发送者") or (
        field == "operator" and value == "未知设置人"
    )

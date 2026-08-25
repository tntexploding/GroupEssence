from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MessageTimeRecord:
    """脱敏的消息时间索引；不携带消息正文或发送者信息。"""

    sender_time: str
    message_id: str = ""
    message_seq: str = ""
    message_random: str = ""


@dataclass
class EssenceMessage:
    sender: str
    sender_time: str
    essence_time: str
    operator: str
    content_text: str
    content_type: str = "text"
    image_path: str = ""
    ocr_text: str = ""
    sender_id: str = ""
    operator_id: str = ""
    group_id: str = ""
    message_id: str = ""
    source: str = "unknown"
    raw_data: dict[str, Any] | None = None

    def normalized_content_for_search(self) -> str:
        parts: list[str] = []
        for value in (self.content_text, self.ocr_text):
            value = value.strip()
            if value and value not in parts:
                parts.append(value)
        return "\n".join(parts)

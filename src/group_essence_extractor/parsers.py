from __future__ import annotations

import hashlib
from pathlib import Path
import re

from .models import EssenceMessage
from .ocr import image_to_text


SENDER_RE = re.compile(r"(?:发送者|发送人)[:：]\s*(.+)")
SENDER_TIME_RE = re.compile(r"(?:发送时间)[:：]\s*(.+)")
ESSENCE_TIME_RE = re.compile(r"(?:精华时间|设置时间)[:：]\s*(.+)")
OPERATOR_RE = re.compile(r"(?:设置人|操作人)[:：]\s*(.+)")


def _pick(pattern: re.Pattern[str], text: str, default: str = "") -> str:
    m = pattern.search(text)
    return m.group(1).strip() if m else default


def parse_screenshot_to_essence(
    image_path: Path,
    ocr_lang: str,
    tesseract_cmd: str,
    group_id: str = "",
) -> EssenceMessage:
    text = image_to_text(image_path, lang=ocr_lang, tesseract_cmd=tesseract_cmd)

    sender = _pick(SENDER_RE, text, "未知发送者")
    sender_time = _pick(SENDER_TIME_RE, text, "")
    essence_time = _pick(ESSENCE_TIME_RE, text, "")
    operator = _pick(OPERATOR_RE, text, "未知设置人")

    # 兜底：若规则未匹配到时间，退化为首个日期时间片段。
    if not sender_time:
        m = re.search(r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}.*?\d{1,2}:\d{2}(?::\d{2})?", text)
        sender_time = m.group(0).strip() if m else ""
    if not essence_time:
        essence_time = sender_time

    # 暂时将 OCR 全文作为可检索内容；后续可替换为结构化区域 OCR。
    content_text = text
    content_type = "image" if "图片" in text else "text"

    return EssenceMessage(
        sender=sender,
        sender_time=sender_time,
        essence_time=essence_time,
        operator=operator,
        content_text=content_text,
        content_type=content_type,
        image_path=str(image_path),
        ocr_text=text,
        group_id=group_id,
        message_id=f"ocr:{_file_sha256(image_path)}",
        source="ocr_screenshot",
        raw_data={"screenshot": str(image_path), "ocr_text": text},
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

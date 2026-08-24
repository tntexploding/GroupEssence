from __future__ import annotations

import hashlib
from pathlib import Path
import re

from .models import EssenceMessage
from .ocr import image_to_result


SENDER_RE = re.compile(r"(?:发送者|发送人)\s*[:：]\s*([^\n]+)")
SENDER_TIME_RE = re.compile(r"发送时间\s*[:：]\s*([^\n]+)")
ESSENCE_TIME_RE = re.compile(r"(?:精华时间|设置时间)\s*[:：]\s*([^\n]+)")
OPERATOR_RE = re.compile(r"(?:设置人|操作人)\s*[:：]\s*([^\n]+)")
FIELD_LINE_RE = re.compile(
    r"^(?:发送者|发送人|发送时间|精华时间|设置时间|设置人|操作人)\s*[:：]"
)
DATE_TOKEN_RE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})\s*[-/.年]\s*(?P<month>\d{1,2})\s*[-/.月]\s*"
    r"(?P<day>\d{1,2})\s*日?"
    r"(?:\s+(?P<hour>\d{1,2})\s*[:：]\s*(?P<minute>\d{2})"
    r"(?:\s*[:：]\s*(?P<second>\d{2}))?)?(?!\d)"
)
INLINE_OPERATOR_RE = re.compile(
    r"由\s*(.+?)\s*(?:设置为精华|设为精华|设置精华|设置|操作)"
)
METADATA_MARKERS = ("发送", "发布", "精华", "设置", "由")
UNKNOWN_SENDER = "未知发送者"
UNKNOWN_OPERATOR = "未知设置人"


def _pick(pattern: re.Pattern[str], text: str, default: str = "") -> str:
    match = pattern.search(text)
    return match.group(1).strip() if match else default


def _clean_line(line: str) -> str:
    line = re.sub(r"[\t\r ]+", " ", line).strip()
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", line)


def _normalized_lines(text: str) -> list[str]:
    return [cleaned for line in text.splitlines() if (cleaned := _clean_line(line))]


def _format_date(match: re.Match[str]) -> str:
    date = (
        f"{int(match.group('year')):04d}-{int(match.group('month')):02d}-"
        f"{int(match.group('day')):02d}"
    )
    hour = match.group("hour")
    if hour is None:
        return date
    second = int(match.group("second") or 0)
    return f"{date} {int(hour):02d}:{int(match.group('minute')):02d}:{second:02d}"


def _extract_dates(text: str) -> list[str]:
    return [_format_date(match) for match in DATE_TOKEN_RE.finditer(text)]


def _normalize_date_field(value: str) -> str:
    match = DATE_TOKEN_RE.search(value)
    return _format_date(match) if match else value.strip()


def _find_metadata_line(lines: list[str]) -> tuple[int | None, list[str]]:
    for index, line in enumerate(lines):
        dates = _extract_dates(line)
        if dates and (len(dates) >= 2 or any(marker in line for marker in METADATA_MARKERS)):
            return index, dates
    return None, []


def _body_from_lines(
    lines: list[str],
    parser_profile: str,
    metadata_index: int | None,
) -> str:
    if parser_profile == "qq_essence_card" and metadata_index is not None:
        body_lines = lines[metadata_index + 1 :]
    elif parser_profile == "labeled":
        body_lines = [line for line in lines if not FIELD_LINE_RE.match(line)]
    else:
        body_lines = lines
    return "\n".join(body_lines).strip()


def parse_screenshot_to_essence(
    image_path: Path,
    ocr_lang: str,
    tesseract_cmd: str,
    group_id: str = "",
) -> EssenceMessage:
    ocr_result = image_to_result(image_path, lang=ocr_lang, tesseract_cmd=tesseract_cmd)
    text = ocr_result.text
    lines = _normalized_lines(text)
    normalized_text = "\n".join(lines)

    sender = _pick(SENDER_RE, normalized_text, UNKNOWN_SENDER)
    sender_time = _normalize_date_field(_pick(SENDER_TIME_RE, normalized_text, ""))
    essence_time = _normalize_date_field(_pick(ESSENCE_TIME_RE, normalized_text, ""))
    operator = _pick(OPERATOR_RE, normalized_text, UNKNOWN_OPERATOR)
    parser_profile = "labeled" if any(
        pattern.search(normalized_text)
        for pattern in (SENDER_RE, SENDER_TIME_RE, ESSENCE_TIME_RE, OPERATOR_RE)
    ) else "fallback"

    metadata_index, metadata_dates = _find_metadata_line(lines)
    if metadata_index is not None:
        if parser_profile == "fallback":
            parser_profile = "qq_essence_card"
        if sender == UNKNOWN_SENDER and metadata_index > 0:
            sender = lines[metadata_index - 1]
        if not sender_time and metadata_dates:
            sender_time = metadata_dates[0]
        if not essence_time and metadata_dates:
            essence_time = metadata_dates[1] if len(metadata_dates) > 1 else metadata_dates[0]
        if operator == UNKNOWN_OPERATOR:
            inline_operator = INLINE_OPERATOR_RE.search(lines[metadata_index])
            if inline_operator:
                operator = inline_operator.group(1).strip(" ：:·|-")

    all_dates = _extract_dates(normalized_text)
    if not sender_time and all_dates:
        sender_time = all_dates[0]
    if not essence_time:
        essence_time = all_dates[1] if len(all_dates) > 1 else sender_time

    content_text = _body_from_lines(lines, parser_profile, metadata_index)
    if not content_text:
        content_text = normalized_text
    content_type = "image" if "图片" in content_text else "text"

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
        raw_data={
            "screenshot": str(image_path),
            "ocr_text": text,
            "parser_profile": parser_profile,
            "ocr_profile": ocr_result.profile,
            "ocr_mean_confidence": ocr_result.mean_confidence,
            "ocr_word_count": ocr_result.word_count,
        },
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

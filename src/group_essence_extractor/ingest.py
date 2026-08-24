from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .config import Settings
from .db import EssenceRepository, SaveStats
from .fetchers import OneBotClient
from .models import EssenceMessage
from .parsers import parse_screenshot_to_essence


def ingest_all(
    settings: Settings,
    repo: EssenceRepository | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    fetched: list[EssenceMessage] = []
    onebot_count = 0
    ocr_count = 0
    onebot_error = ""
    ocr_error_count = 0

    if settings.prefer_onebot:
        try:
            with OneBotClient(settings.onebot_base_url, settings.onebot_access_token) as client:
                fetched = client.get_essence_messages(group_id=settings.group_id)
            onebot_count = len(fetched)
        except Exception as exc:
            onebot_error = str(exc)
            fetched = []

    if (not fetched) and settings.fallback_ocr:
        fetched, ocr_error_count = ingest_from_screenshots(
            settings.screenshot_dir,
            settings.ocr_lang,
            settings.tesseract_cmd,
            settings.group_id,
        )
        ocr_count = len(fetched)

    if dry_run:
        save_stats = SaveStats()
    else:
        if repo is None:
            raise ValueError("非 dry-run 采集必须提供数据库仓库")
        save_stats = repo.upsert_messages(fetched)

    return {
        "dry_run": dry_run,
        "collected": len(fetched),
        "from_onebot": onebot_count,
        "onebot_error": onebot_error,
        "from_ocr": ocr_count,
        "ocr_error_count": ocr_error_count,
        "quality": summarize_messages(fetched),
        **save_stats.as_dict(),
    }


def summarize_messages(messages: list[EssenceMessage]) -> dict[str, Any]:
    missing_fields = {
        field: sum(_field_is_missing(message, field) for message in messages)
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
                not any(_field_is_missing(message, field) for field in structured_fields)
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


def _field_is_missing(message: EssenceMessage, field: str) -> bool:
    value = str(getattr(message, field, "") or "").strip()
    if not value:
        return True
    return (field == "sender" and value == "未知发送者") or (
        field == "operator" and value == "未知设置人"
    )


def ingest_from_screenshots(
    screenshot_dir: Path,
    ocr_lang: str,
    tesseract_cmd: str,
    group_id: str = "",
    limit: int | None = None,
) -> tuple[list[EssenceMessage], int]:
    if limit is not None and limit <= 0:
        raise ValueError("OCR 预览数量必须大于 0")

    messages: list[EssenceMessage] = []
    error_count = 0
    candidates = list_screenshot_candidates(screenshot_dir)
    if limit is not None:
        candidates = candidates[:limit]
    for image_path in candidates:
        try:
            messages.append(
                parse_screenshot_to_essence(
                    image_path=image_path,
                    ocr_lang=ocr_lang,
                    tesseract_cmd=tesseract_cmd,
                    group_id=group_id,
                )
            )
        except Exception:
            error_count += 1
            continue
    return messages, error_count


def list_screenshot_candidates(screenshot_dir: Path) -> list[Path]:
    if not screenshot_dir.is_dir():
        return []
    supported_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
    return sorted(
        (
            path
            for path in screenshot_dir.iterdir()
            if path.is_file() and path.suffix.lower() in supported_suffixes
        ),
        key=lambda path: path.name.casefold(),
    )

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
        field: sum(not str(getattr(message, field, "") or "").strip() for message in messages)
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
    }


def ingest_from_screenshots(
    screenshot_dir: Path,
    ocr_lang: str,
    tesseract_cmd: str,
    group_id: str = "",
) -> tuple[list[EssenceMessage], int]:
    if not screenshot_dir.exists():
        return [], 0

    messages: list[EssenceMessage] = []
    error_count = 0
    for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
        for image_path in sorted(screenshot_dir.glob(ext)):
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

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Settings
from .db import EssenceRepository
from .fetchers import OneBotClient
from .models import EssenceMessage
from .parsers import parse_screenshot_to_essence


def ingest_all(settings: Settings, repo: EssenceRepository) -> dict[str, Any]:
    fetched: list[EssenceMessage] = []
    onebot_count = 0
    ocr_count = 0
    onebot_error = ""
    ocr_error_count = 0

    if settings.prefer_onebot:
        try:
            client = OneBotClient(settings.onebot_base_url, settings.onebot_access_token)
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
        )
        ocr_count = len(fetched)

    inserted = repo.insert_messages(fetched)
    return {
        "from_onebot": onebot_count,
        "onebot_error": onebot_error,
        "from_ocr": ocr_count,
        "ocr_error_count": ocr_error_count,
        "inserted": inserted,
    }


def ingest_from_screenshots(
    screenshot_dir: Path,
    ocr_lang: str,
    tesseract_cmd: str,
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
                    )
                )
            except Exception:
                error_count += 1
                continue
    return messages, error_count

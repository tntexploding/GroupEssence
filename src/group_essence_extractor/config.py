from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    db_path: Path
    onebot_base_url: str
    onebot_access_token: str
    group_id: str
    prefer_onebot: bool
    fallback_ocr: bool
    ocr_lang: str
    tesseract_cmd: str
    screenshot_dir: Path
    image_dir: Path


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_settings() -> Settings:
    return Settings(
        db_path=Path(os.getenv("DB_PATH", "./data/group_essence.db")).resolve(),
        onebot_base_url=os.getenv("ONEBOT_BASE_URL", "http://127.0.0.1:3000").strip(),
        onebot_access_token=os.getenv("ONEBOT_ACCESS_TOKEN", "").strip(),
        group_id=os.getenv("GROUP_ID", "").strip(),
        prefer_onebot=_as_bool(os.getenv("PREFER_ONEBOT"), True),
        fallback_ocr=_as_bool(os.getenv("FALLBACK_OCR"), True),
        ocr_lang=os.getenv("OCR_LANG", "chi_sim+eng").strip(),
        tesseract_cmd=os.getenv("TESSERACT_CMD", "").strip(),
        screenshot_dir=Path(os.getenv("SCREENSHOT_DIR", "./data/screenshots")).resolve(),
        image_dir=Path(os.getenv("IMAGE_DIR", "./data/images")).resolve(),
    )

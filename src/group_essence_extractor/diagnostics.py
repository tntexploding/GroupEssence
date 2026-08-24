from __future__ import annotations

import os
from pathlib import Path
import platform
import sys
from typing import Any

from .config import Settings
from .ocr import resolve_tesseract_command


def run_doctor(settings: Settings) -> dict[str, Any]:
    """执行不联网、不写文件的本地运行条件检查。"""
    checks: list[dict[str, str]] = []

    python_ok = sys.version_info >= (3, 10)
    checks.append(
        _check(
            "python",
            "ok" if python_ok else "error",
            f"Python {platform.python_version()} ({platform.system()})",
        )
    )

    existing_parent = _nearest_existing_parent(settings.db_path.parent)
    parent_writable = existing_parent is not None and os.access(existing_parent, os.W_OK)
    if settings.db_path.exists():
        database_message = f"数据库已存在: {settings.db_path}"
    else:
        database_message = f"数据库将在首次写入时创建: {settings.db_path}"
    checks.append(
        _check(
            "database",
            "ok" if parent_writable else "error",
            database_message if parent_writable else f"数据库目录不可写: {settings.db_path.parent}",
        )
    )

    if settings.prefer_onebot:
        missing = []
        if not settings.onebot_base_url:
            missing.append("ONEBOT_BASE_URL")
        if not settings.group_id:
            missing.append("GROUP_ID")
        checks.append(
            _check(
                "onebot_config",
                "error" if missing else "ok",
                f"缺少配置: {', '.join(missing)}"
                if missing
                else f"OneBot 参数已配置（未联网检查）: {settings.onebot_base_url}",
            )
        )
    else:
        checks.append(_check("onebot_config", "skipped", "PREFER_ONEBOT=false"))

    if settings.fallback_ocr:
        tesseract = resolve_tesseract_command(settings.tesseract_cmd)
        tesseract_ok = tesseract is not None and tesseract.is_file()
        checks.append(
            _check(
                "tesseract",
                "ok" if tesseract_ok else "error",
                f"Tesseract: {tesseract}"
                if tesseract_ok
                else "未找到 Tesseract，请配置 TESSERACT_CMD 或 PATH",
            )
        )
        if settings.screenshot_dir.is_dir():
            image_count = sum(
                1
                for path in settings.screenshot_dir.iterdir()
                if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            )
            checks.append(
                _check(
                    "screenshot_dir",
                    "ok",
                    f"截图目录可用，共 {image_count} 个候选文件: {settings.screenshot_dir}",
                )
            )
        else:
            checks.append(
                _check(
                    "screenshot_dir",
                    "warning",
                    f"截图目录尚不存在: {settings.screenshot_dir}",
                )
            )
    else:
        checks.append(_check("tesseract", "skipped", "FALLBACK_OCR=false"))
        checks.append(_check("screenshot_dir", "skipped", "FALLBACK_OCR=false"))

    if not settings.prefer_onebot and not settings.fallback_ocr:
        checks.append(_check("ingest_source", "error", "OneBot 与 OCR 均已关闭"))

    statuses = {check["status"] for check in checks}
    status = "error" if "error" in statuses else "warning" if "warning" in statuses else "ok"
    return {"status": status, "checks": checks}


def _check(name: str, status: str, message: str) -> dict[str, str]:
    return {"name": name, "status": status, "message": message}


def _nearest_existing_parent(path: Path) -> Path | None:
    candidate = path.resolve()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return candidate if candidate.is_dir() else None

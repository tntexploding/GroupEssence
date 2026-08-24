from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Callable
from urllib.parse import urlparse

from PIL import Image
import requests

from .db import EssenceRepository
from .ocr import image_to_text


DEFAULT_MAX_IMAGE_BYTES = 20 * 1024 * 1024
COMPLETED_STATUSES = {"completed", "no_text"}
IMAGE_EXTENSIONS = {
    "BMP": ".bmp",
    "GIF": ".gif",
    "JPEG": ".jpg",
    "PNG": ".png",
    "TIFF": ".tiff",
    "WEBP": ".webp",
}


@dataclass(frozen=True)
class DownloadedImage:
    absolute_path: Path
    local_path: str
    content_sha256: str
    mime_type: str
    byte_size: int
    downloaded: bool
    deduplicated: bool


def validate_enrichment_options(
    limit: int,
    timeout_seconds: float,
    max_bytes: int,
) -> None:
    if limit < 1:
        raise ValueError("--limit 必须大于 0")
    if timeout_seconds <= 0:
        raise ValueError("--timeout 必须大于 0")
    if max_bytes < 1:
        raise ValueError("--max-bytes 必须大于 0")


def enrich_images(
    repository: EssenceRepository,
    image_dir: Path,
    ocr_lang: str,
    tesseract_cmd: str = "",
    *,
    apply: bool = False,
    group_id: str = "",
    limit: int = 100,
    timeout_seconds: float = 20,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    session: requests.Session | None = None,
    ocr_reader: Callable[[Path, str, str], str] | None = None,
) -> dict[str, Any]:
    """预览或执行 OneBot 图片下载与 OCR；预览阶段不联网、不写文件。"""
    validate_enrichment_options(limit, timeout_seconds, max_bytes)
    if not repository.db_path.is_file():
        return {
            "status": "error",
            "database": str(repository.db_path),
            "dry_run": not apply,
            "error": "数据库文件不存在",
        }

    try:
        messages = repository.list_image_messages(group_id=group_id)
        existing_rows = repository.list_image_attachments(
            int(message["id"]) for message in messages
        )
    except (OSError, sqlite3.Error) as exc:
        return {
            "status": "error",
            "database": str(repository.db_path),
            "dry_run": not apply,
            "error": str(exc),
        }

    existing = {
        (int(row["essence_id"]), int(row["position"])): row
        for row in existing_rows
    }
    candidates: list[dict[str, Any]] = []
    unsupported = 0
    for message in messages:
        urls, invalid_count = _extract_http_urls(str(message.get("image_path") or ""))
        unsupported += invalid_count
        for position, remote_url in enumerate(urls):
            candidates.append(
                {
                    "essence_id": int(message["id"]),
                    "position": position,
                    "remote_url": remote_url,
                }
            )

    pending = [
        candidate
        for candidate in candidates
        if str(
            existing.get((candidate["essence_id"], candidate["position"]), {}).get(
                "status", ""
            )
        )
        not in COMPLETED_STATUSES
    ]
    selected = pending[:limit]
    report: dict[str, Any] = {
        "status": "ok",
        "database": str(repository.db_path),
        "image_dir": str(image_dir.resolve()),
        "dry_run": not apply,
        "scanned_messages": len(messages),
        "discovered": len(candidates),
        "unsupported": unsupported,
        "already_complete": len(candidates) - len(pending),
        "pending": len(pending),
        "selected": len(selected),
        "processed": 0,
        "downloaded": 0,
        "cache_hits": 0,
        "deduplicated_files": 0,
        "ocr_completed": 0,
        "no_text": 0,
        "failed": 0,
        "remaining": len(pending),
    }
    if not apply or not selected:
        return report

    reader = ocr_reader or image_to_text
    owns_session = session is None
    current_session = session or requests.Session()
    try:
        for candidate in selected:
            report["processed"] += 1
            key = (candidate["essence_id"], candidate["position"])
            old = existing.get(key, {})
            downloaded_image: DownloadedImage | None = None
            try:
                downloaded_image = _existing_cached_image(old, image_dir)
                if downloaded_image is None:
                    downloaded_image = _download_image(
                        current_session,
                        candidate["remote_url"],
                        image_dir,
                        timeout_seconds,
                        max_bytes,
                    )
                if downloaded_image.downloaded:
                    report["downloaded"] += 1
                else:
                    report["cache_hits"] += 1
                if downloaded_image.deduplicated:
                    report["deduplicated_files"] += 1

                ocr_text = reader(
                    downloaded_image.absolute_path,
                    ocr_lang,
                    tesseract_cmd,
                ).strip()
                status = "completed" if ocr_text else "no_text"
                repository.save_image_attachment(
                    essence_id=candidate["essence_id"],
                    position=candidate["position"],
                    remote_url=candidate["remote_url"],
                    local_path=downloaded_image.local_path,
                    content_sha256=downloaded_image.content_sha256,
                    mime_type=downloaded_image.mime_type,
                    byte_size=downloaded_image.byte_size,
                    ocr_text=ocr_text,
                    status=status,
                )
                report["ocr_completed" if ocr_text else "no_text"] += 1
            except Exception as exc:
                repository.save_image_attachment(
                    essence_id=candidate["essence_id"],
                    position=candidate["position"],
                    remote_url=candidate["remote_url"],
                    local_path=(
                        downloaded_image.local_path
                        if downloaded_image is not None
                        else str(old.get("local_path") or "")
                    ),
                    content_sha256=(
                        downloaded_image.content_sha256
                        if downloaded_image is not None
                        else str(old.get("content_sha256") or "")
                    ),
                    mime_type=(
                        downloaded_image.mime_type
                        if downloaded_image is not None
                        else str(old.get("mime_type") or "")
                    ),
                    byte_size=(
                        downloaded_image.byte_size
                        if downloaded_image is not None
                        else int(old.get("byte_size") or 0)
                    ),
                    ocr_text=str(old.get("ocr_text") or ""),
                    status="failed",
                    error=str(exc)[:1000],
                )
                report["failed"] += 1
    finally:
        if owns_session:
            current_session.close()

    report["remaining"] = len(pending) - len(selected) + report["failed"]
    if report["failed"]:
        report["status"] = "warning"
    return report


def _extract_http_urls(value: str) -> tuple[list[str], int]:
    urls: list[str] = []
    unsupported = 0
    for line in value.splitlines():
        candidate = line.strip()
        if not candidate or candidate in urls:
            continue
        if urlparse(candidate).scheme.lower() not in {"http", "https"}:
            unsupported += 1
            continue
        urls.append(candidate)
    return urls, unsupported


def _existing_cached_image(row: dict[str, Any], image_dir: Path) -> DownloadedImage | None:
    local_path = str(row.get("local_path") or "").strip()
    content_sha256 = str(row.get("content_sha256") or "").strip()
    if not local_path or not content_sha256:
        return None
    image_root = image_dir.resolve()
    candidate = (image_root / Path(local_path)).resolve()
    try:
        candidate.relative_to(image_root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return DownloadedImage(
        absolute_path=candidate,
        local_path=local_path,
        content_sha256=content_sha256,
        mime_type=str(row.get("mime_type") or ""),
        byte_size=int(row.get("byte_size") or candidate.stat().st_size),
        downloaded=False,
        deduplicated=False,
    )


def _download_image(
    session: requests.Session,
    remote_url: str,
    image_dir: Path,
    timeout_seconds: float,
    max_bytes: int,
) -> DownloadedImage:
    payload = bytearray()
    response_mime = ""
    with session.get(remote_url, stream=True, timeout=timeout_seconds) as response:
        response.raise_for_status()
        response_mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        content_length = response.headers.get("Content-Length", "").strip()
        if content_length.isdigit() and int(content_length) > max_bytes:
            raise ValueError(f"图片超过大小限制 {max_bytes} bytes")
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise ValueError(f"图片超过大小限制 {max_bytes} bytes")

    with Image.open(BytesIO(payload)) as image:
        image_format = str(image.format or "").upper()
        image.verify()
    extension = IMAGE_EXTENSIONS.get(image_format, ".img")
    inferred_mime = Image.MIME.get(image_format, "application/octet-stream")
    mime_type = response_mime if response_mime.startswith("image/") else inferred_mime
    content_sha256 = hashlib.sha256(payload).hexdigest()
    relative_path = Path(content_sha256[:2]) / f"{content_sha256}{extension}"
    destination = image_dir.resolve() / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    deduplicated = destination.is_file()
    if not deduplicated:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".image-",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary_path = Path(temporary.name)
            temporary_path.replace(destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    return DownloadedImage(
        absolute_path=destination,
        local_path=relative_path.as_posix(),
        content_sha256=content_sha256,
        mime_type=mime_type,
        byte_size=len(payload),
        downloaded=True,
        deduplicated=deduplicated,
    )

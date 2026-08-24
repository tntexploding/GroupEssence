from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .db import EssenceRepository


CSV_COLUMNS = (
    "id",
    "group_id",
    "message_id",
    "sender",
    "sender_id",
    "sender_time",
    "essence_time",
    "operator",
    "operator_id",
    "content_text",
    "content_type",
    "image_path",
    "ocr_text",
    "source",
    "created_at",
)


def export_records(
    repository: EssenceRepository,
    output_path: Path,
    output_format: str,
    filters: dict[str, str] | None = None,
    max_records: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """将搜索结果导出到 JSON 或 CSV，并返回不含消息正文的摘要。"""
    output_format = output_format.lower().strip()
    if output_format not in {"json", "csv"}:
        raise ValueError("导出格式必须是 json 或 csv")
    if max_records is not None and max_records < 1:
        raise ValueError("--max-records 必须大于 0")

    output_path = output_path.resolve()
    if output_path == repository.db_path.resolve():
        raise ValueError("导出路径不能覆盖当前数据库")
    if output_path.exists() and not force:
        raise FileExistsError(f"输出文件已存在；如需覆盖请添加 --force: {output_path}")

    query = dict(filters or {})
    items: list[dict[str, Any]] = []
    offset = 0
    total = 0
    while True:
        remaining = None if max_records is None else max_records - len(items)
        if remaining is not None and remaining <= 0:
            break
        batch_limit = min(500, remaining) if remaining is not None else 500
        page = repository.search_page(limit=batch_limit, offset=offset, **query)
        total = page.total
        items.extend(page.items)
        offset += len(page.items)
        if not page.items or offset >= total:
            break

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        payload = {"total": total, "exported": len(items), "items": items}
        with output_path.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
    else:
        with output_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(items)

    return {
        "status": "ok",
        "format": output_format,
        "output": str(output_path),
        "total": total,
        "exported": len(items),
    }

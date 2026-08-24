from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .models import EssenceMessage


SCHEMA_VERSION = 1

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS essence_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT,
    message_id TEXT,
    sender TEXT NOT NULL,
    sender_id TEXT,
    sender_time TEXT NOT NULL,
    essence_time TEXT NOT NULL,
    operator TEXT NOT NULL,
    operator_id TEXT,
    content_text TEXT NOT NULL,
    content_type TEXT NOT NULL,
    image_path TEXT,
    ocr_text TEXT,
    content_search TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_json TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(group_id, message_id, sender_time, essence_time, sender, operator, content_text)
);
"""

CREATE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_sender ON essence_messages(sender);",
    "CREATE INDEX IF NOT EXISTS idx_sender_id ON essence_messages(sender_id);",
    "CREATE INDEX IF NOT EXISTS idx_operator ON essence_messages(operator);",
    "CREATE INDEX IF NOT EXISTS idx_operator_id ON essence_messages(operator_id);",
    "CREATE INDEX IF NOT EXISTS idx_sender_time ON essence_messages(sender_time);",
    "CREATE INDEX IF NOT EXISTS idx_essence_time ON essence_messages(essence_time);",
    "CREATE INDEX IF NOT EXISTS idx_source_message ON essence_messages(source, group_id, message_id);",
    "CREATE INDEX IF NOT EXISTS idx_source_image ON essence_messages(source, image_path);",
]

MESSAGE_COLUMNS = (
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
    "content_search",
    "source",
    "raw_json",
)


@dataclass(frozen=True)
class SaveStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
        }


@dataclass(frozen=True)
class MigrationStats:
    from_version: int
    to_version: int
    applied: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "applied": list(self.applied),
        }


@dataclass(frozen=True)
class SearchPage:
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "count": len(self.items),
            "limit": self.limit,
            "offset": self.offset,
            "items": self.items,
        }


class EssenceRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def _connect(self, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> MigrationStats:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            from_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if from_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {from_version} 高于程序支持的版本 {SCHEMA_VERSION}"
                )

            applied: list[int] = []
            if from_version < 1:
                conn.execute(CREATE_TABLE_SQL)
                for sql in CREATE_INDEX_SQL:
                    conn.execute(sql)
                conn.execute("PRAGMA user_version = 1")
                applied.append(1)

            return MigrationStats(
                from_version=from_version,
                to_version=SCHEMA_VERSION,
                applied=tuple(applied),
            )

    def audit(self) -> dict[str, Any]:
        """以只读连接汇总数据质量，不创建或修改数据库。"""
        if not self.db_path.is_file():
            return {
                "status": "error",
                "database": str(self.db_path),
                "error": "数据库文件不存在",
            }

        try:
            with closing(self._connect(read_only=True)) as conn:
                table_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'essence_messages'"
                ).fetchone()
                if table_exists is None:
                    return {
                        "status": "error",
                        "database": str(self.db_path),
                        "error": "数据库缺少 essence_messages 表",
                    }

                schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
                total = int(conn.execute("SELECT COUNT(*) FROM essence_messages").fetchone()[0])
                missing = {
                    field: int(
                        conn.execute(
                            f"""
                            SELECT COUNT(*) FROM essence_messages
                            WHERE {field} IS NULL OR TRIM(CAST({field} AS TEXT)) = ''
                            """
                        ).fetchone()[0]
                    )
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
                duplicate_message_identities = int(
                    conn.execute(
                        """
                        SELECT COALESCE(SUM(count_per_identity - 1), 0)
                        FROM (
                            SELECT COUNT(*) AS count_per_identity
                            FROM essence_messages
                            WHERE message_id IS NOT NULL AND TRIM(message_id) <> ''
                            GROUP BY source, group_id, message_id
                            HAVING COUNT(*) > 1
                        )
                        """
                    ).fetchone()[0]
                )
                duplicate_ocr_paths = int(
                    conn.execute(
                        """
                        SELECT COALESCE(SUM(count_per_path - 1), 0)
                        FROM (
                            SELECT COUNT(*) AS count_per_path
                            FROM essence_messages
                            WHERE source = 'ocr_screenshot'
                              AND image_path IS NOT NULL
                              AND TRIM(image_path) <> ''
                            GROUP BY group_id, image_path
                            HAVING COUNT(*) > 1
                        )
                        """
                    ).fetchone()[0]
                )
                sender_range = conn.execute(
                    "SELECT MIN(NULLIF(sender_time, '')), MAX(NULLIF(sender_time, '')) FROM essence_messages"
                ).fetchone()
                essence_range = conn.execute(
                    "SELECT MIN(NULLIF(essence_time, '')), MAX(NULLIF(essence_time, '')) FROM essence_messages"
                ).fetchone()

                return {
                    "status": "ok" if integrity == "ok" else "error",
                    "database": str(self.db_path),
                    "size_bytes": self.db_path.stat().st_size,
                    "integrity": integrity,
                    "schema_version": schema_version,
                    "supported_schema_version": SCHEMA_VERSION,
                    "migration_required": schema_version < SCHEMA_VERSION,
                    "total": total,
                    "by_source": self._count_by(conn, "source"),
                    "by_content_type": self._count_by(conn, "content_type"),
                    "missing": missing,
                    "duplicates": {
                        "message_identity": duplicate_message_identities,
                        "ocr_image_path": duplicate_ocr_paths,
                    },
                    "time_ranges": {
                        "sender_time": {"min": sender_range[0], "max": sender_range[1]},
                        "essence_time": {"min": essence_range[0], "max": essence_range[1]},
                    },
                }
        except sqlite3.Error as exc:
            return {
                "status": "error",
                "database": str(self.db_path),
                "error": str(exc),
            }

    @staticmethod
    def _count_by(conn: sqlite3.Connection, column: str) -> dict[str, int]:
        rows = conn.execute(
            f"SELECT COALESCE({column}, ''), COUNT(*) FROM essence_messages GROUP BY {column}"
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def upsert_messages(self, messages: Iterable[EssenceMessage]) -> SaveStats:
        insert_sql = """
        INSERT OR IGNORE INTO essence_messages (
            group_id, message_id, sender, sender_id, sender_time, essence_time,
            operator, operator_id, content_text, content_type, image_path,
            ocr_text, content_search, source, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        inserted = 0
        updated = 0
        unchanged = 0
        with closing(self._connect()) as conn, conn:
            for msg in messages:
                values = self._message_values(msg)
                existing = self._find_existing(conn, msg)
                if existing is not None:
                    old_values = tuple(
                        existing[column] if existing[column] is not None else ""
                        for column in MESSAGE_COLUMNS
                    )
                    new_values = tuple(values[column] for column in MESSAGE_COLUMNS)
                    if old_values == new_values:
                        unchanged += 1
                        continue

                    assignments = ", ".join(f"{column} = ?" for column in MESSAGE_COLUMNS)
                    conn.execute(
                        f"UPDATE essence_messages SET {assignments} WHERE id = ?",
                        (*new_values, existing["id"]),
                    )
                    updated += 1
                    continue

                cursor = conn.execute(
                    insert_sql,
                    tuple(values[column] for column in MESSAGE_COLUMNS),
                )
                if cursor.rowcount > 0:
                    inserted += 1
                else:
                    unchanged += 1
        return SaveStats(inserted=inserted, updated=updated, unchanged=unchanged)

    def insert_messages(self, messages: Iterable[EssenceMessage]) -> int:
        """兼容旧调用方，仅返回实际新增记录数。"""
        return self.upsert_messages(messages).inserted

    @staticmethod
    def _message_values(msg: EssenceMessage) -> dict[str, str]:
        return {
            "group_id": msg.group_id,
            "message_id": msg.message_id,
            "sender": msg.sender,
            "sender_id": msg.sender_id,
            "sender_time": msg.sender_time,
            "essence_time": msg.essence_time,
            "operator": msg.operator,
            "operator_id": msg.operator_id,
            "content_text": msg.content_text,
            "content_type": msg.content_type,
            "image_path": msg.image_path,
            "ocr_text": msg.ocr_text,
            "content_search": msg.normalized_content_for_search(),
            "source": msg.source,
            "raw_json": json.dumps(msg.raw_data or {}, ensure_ascii=False, sort_keys=True),
        }

    @staticmethod
    def _find_existing(
        conn: sqlite3.Connection,
        msg: EssenceMessage,
    ) -> sqlite3.Row | None:
        if msg.source == "ocr_screenshot" and msg.image_path:
            return conn.execute(
                f"""
                SELECT id, {", ".join(MESSAGE_COLUMNS)}
                FROM essence_messages
                WHERE source = ?
                  AND ((message_id = ? AND message_id <> '') OR image_path = ?)
                  AND (group_id = ? OR group_id = '')
                ORDER BY CASE WHEN group_id = ? THEN 0 ELSE 1 END, id
                LIMIT 1
                """,
                (msg.source, msg.message_id, msg.image_path, msg.group_id, msg.group_id),
            ).fetchone()

        if msg.message_id:
            return conn.execute(
                f"""
                SELECT id, {", ".join(MESSAGE_COLUMNS)}
                FROM essence_messages
                WHERE source = ? AND message_id = ? AND (group_id = ? OR group_id = '')
                ORDER BY CASE WHEN group_id = ? THEN 0 ELSE 1 END, id
                LIMIT 1
                """,
                (msg.source, msg.message_id, msg.group_id, msg.group_id),
            ).fetchone()
        return None

    def repair(self, default_group_id: str = "", apply: bool = False) -> dict[str, Any]:
        """预览或修复可从 raw_json 确定恢复的旧记录字段。"""
        if not self.db_path.is_file():
            return {
                "status": "error",
                "database": str(self.db_path),
                "dry_run": not apply,
                "error": "数据库文件不存在",
            }

        repair_fields = (
            "group_id",
            "message_id",
            "sender_time",
            "essence_time",
            "content_search",
        )
        unresolved_fields = repair_fields[:-1]
        candidates = {field: 0 for field in repair_fields}
        unresolved = {field: 0 for field in unresolved_fields}

        try:
            with closing(self._connect(read_only=not apply)) as conn:
                table_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'essence_messages'"
                ).fetchone()
                if table_exists is None:
                    return {
                        "status": "error",
                        "database": str(self.db_path),
                        "dry_run": not apply,
                        "error": "数据库缺少 essence_messages 表",
                    }

                rows = conn.execute(
                    """
                    SELECT id, group_id, message_id, sender_time, essence_time,
                           content_text, ocr_text, content_search, source, raw_json
                    FROM essence_messages
                    ORDER BY id
                    """
                ).fetchall()
                pending: list[tuple[int, dict[str, str]]] = []
                for row in rows:
                    updates = _repair_updates(row, default_group_id.strip())
                    if updates:
                        pending.append((int(row["id"]), updates))
                        for field in updates:
                            candidates[field] += 1

                    for field in unresolved_fields:
                        if _is_blank(row[field]) and field not in updates:
                            unresolved[field] += 1

                updated = 0
                if apply:
                    with conn:
                        for row_id, updates in pending:
                            assignments = ", ".join(f"{field} = ?" for field in updates)
                            cursor = conn.execute(
                                f"UPDATE essence_messages SET {assignments} WHERE id = ?",
                                (*updates.values(), row_id),
                            )
                            updated += max(0, cursor.rowcount)

                return {
                    "status": "ok",
                    "database": str(self.db_path),
                    "dry_run": not apply,
                    "scanned": len(rows),
                    "candidates": candidates,
                    "unresolved": unresolved,
                    "would_update": len(pending),
                    "updated": updated,
                }
        except sqlite3.Error as exc:
            return {
                "status": "error",
                "database": str(self.db_path),
                "dry_run": not apply,
                "error": str(exc),
            }

    def search(
        self,
        sender_time: str = "",
        essence_time: str = "",
        sender: str = "",
        sender_qq: str = "",
        operator: str = "",
        operator_qq: str = "",
        content: str = "",
        limit: int = 100,
        offset: int = 0,
        group_id: str = "",
        source: str = "",
        content_type: str = "",
        sender_time_from: str = "",
        sender_time_to: str = "",
        essence_time_from: str = "",
        essence_time_to: str = "",
    ) -> list[dict[str, Any]]:
        return self.search_page(
            sender_time=sender_time,
            essence_time=essence_time,
            sender=sender,
            sender_qq=sender_qq,
            operator=operator,
            operator_qq=operator_qq,
            content=content,
            limit=limit,
            offset=offset,
            group_id=group_id,
            source=source,
            content_type=content_type,
            sender_time_from=sender_time_from,
            sender_time_to=sender_time_to,
            essence_time_from=essence_time_from,
            essence_time_to=essence_time_to,
        ).items

    def search_page(
        self,
        sender_time: str = "",
        essence_time: str = "",
        sender: str = "",
        sender_qq: str = "",
        operator: str = "",
        operator_qq: str = "",
        content: str = "",
        limit: int = 100,
        offset: int = 0,
        group_id: str = "",
        source: str = "",
        content_type: str = "",
        sender_time_from: str = "",
        sender_time_to: str = "",
        essence_time_from: str = "",
        essence_time_to: str = "",
    ) -> SearchPage:
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        conditions, params = self._search_conditions(
            sender_time=sender_time,
            essence_time=essence_time,
            sender=sender,
            sender_qq=sender_qq,
            operator=operator,
            operator_qq=operator_qq,
            content=content,
            group_id=group_id,
            source=source,
            content_type=content_type,
            sender_time_from=sender_time_from,
            sender_time_to=sender_time_to,
            essence_time_from=essence_time_from,
            essence_time_to=essence_time_to,
        )
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        select_sql = f"""
        SELECT
            id, group_id, message_id, sender, sender_id, sender_time, essence_time,
            operator, operator_id, content_text, content_type, image_path,
            ocr_text, source, created_at
        FROM essence_messages
        {where_clause}
        ORDER BY essence_time DESC, id DESC
        LIMIT ? OFFSET ?
        """

        with closing(self._connect(read_only=True)) as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM essence_messages {where_clause}",
                    params,
                ).fetchone()[0]
            )
            rows = conn.execute(select_sql, [*params, limit, offset]).fetchall()
        return SearchPage(
            items=[dict(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _search_conditions(**filters: str) -> tuple[list[str], list[str]]:
        conditions: list[str] = []
        params: list[str] = []
        contains_fields = {
            "sender_time": "sender_time",
            "essence_time": "essence_time",
            "sender": "sender",
            "operator": "operator",
            "content": "content_search",
        }
        exact_fields = {
            "sender_qq": "sender_id",
            "operator_qq": "operator_id",
            "group_id": "group_id",
            "source": "source",
            "content_type": "content_type",
        }
        range_fields = {
            "sender_time_from": ("sender_time", ">="),
            "sender_time_to": ("sender_time", "<="),
            "essence_time_from": ("essence_time", ">="),
            "essence_time_to": ("essence_time", "<="),
        }

        for name, column in contains_fields.items():
            value = filters.get(name, "").strip()
            if value:
                conditions.append(f"{column} LIKE ?")
                params.append(f"%{value}%")
        for name, column in exact_fields.items():
            value = filters.get(name, "").strip()
            if value:
                conditions.append(f"{column} = ?")
                params.append(value)
        for name, (column, operator) in range_fields.items():
            value = filters.get(name, "").strip()
            if value:
                conditions.append(f"{column} {operator} ?")
                params.append(value)
        return conditions, params


def _repair_updates(row: sqlite3.Row, default_group_id: str) -> dict[str, str]:
    raw = _load_raw_json(row["raw_json"])
    essence = raw.get("essence") if isinstance(raw.get("essence"), dict) else {}
    detail = raw.get("message_detail") if isinstance(raw.get("message_detail"), dict) else {}
    updates: dict[str, str] = {}

    candidates: dict[str, Any] = {
        "group_id": _first_present(
            raw.get("group_id"), essence.get("group_id"), detail.get("group_id")
        ),
        "message_id": _first_present(
            raw.get("message_id"), essence.get("message_id"), detail.get("message_id")
        ),
        "sender_time": _first_present(
            raw.get("sender_time"), essence.get("sender_time"), detail.get("time")
        ),
        "essence_time": _first_present(
            raw.get("essence_time"),
            raw.get("operator_time"),
            essence.get("essence_time"),
            essence.get("operator_time"),
        ),
    }
    if not candidates["group_id"] and str(row["source"]) == "onebot":
        candidates["group_id"] = default_group_id

    for field, candidate in candidates.items():
        if not _is_blank(row[field]) or _is_blank(candidate):
            continue
        value = str(candidate).strip()
        if field.endswith("_time"):
            value = _normalize_timestamp(candidate)
        if value:
            updates[field] = value

    content_search = _normalized_search_text(row["content_text"], row["ocr_text"])
    if str(row["content_search"] or "") != content_search:
        updates["content_search"] = content_search
    return updates


def _load_raw_json(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_present(*values: Any) -> Any:
    for value in values:
        if not _is_blank(value):
            return value
    return ""


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _normalize_timestamp(value: Any) -> str:
    original = str(value).strip()
    try:
        timestamp = float(original)
        while abs(timestamp) > 253_402_300_799:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return original


def _normalized_search_text(content_text: Any, ocr_text: Any) -> str:
    parts: list[str] = []
    for value in (content_text, ocr_text):
        text = str(value or "").strip()
        if text and text not in parts:
            parts.append(text)
    return "\n".join(parts)

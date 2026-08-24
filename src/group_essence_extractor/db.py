from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import EssenceMessage


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


class EssenceRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(CREATE_TABLE_SQL)
            for sql in CREATE_INDEX_SQL:
                conn.execute(sql)

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
    ) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        conditions: list[str] = []
        params: list[str | int] = []

        if sender_time:
            conditions.append("sender_time LIKE ?")
            params.append(f"%{sender_time}%")
        if essence_time:
            conditions.append("essence_time LIKE ?")
            params.append(f"%{essence_time}%")
        if sender:
            conditions.append("sender LIKE ?")
            params.append(f"%{sender}%")
        if sender_qq:
            conditions.append("sender_id = ?")
            params.append(sender_qq)
        if operator:
            conditions.append("operator LIKE ?")
            params.append(f"%{operator}%")
        if operator_qq:
            conditions.append("operator_id = ?")
            params.append(operator_qq)
        if content:
            conditions.append("content_search LIKE ?")
            params.append(f"%{content}%")

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        sql = f"""
        SELECT
            id, group_id, message_id, sender, sender_id, sender_time, essence_time,
            operator, operator_id, content_text, content_type, image_path,
            ocr_text, source, created_at
        FROM essence_messages
        {where_clause}
        ORDER BY essence_time DESC, id DESC
        LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

from __future__ import annotations

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
]


class EssenceRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(CREATE_TABLE_SQL)
            for sql in CREATE_INDEX_SQL:
                conn.execute(sql)

    def insert_messages(self, messages: Iterable[EssenceMessage]) -> int:
        insert_sql = """
        INSERT OR IGNORE INTO essence_messages (
            group_id, message_id, sender, sender_id, sender_time, essence_time,
            operator, operator_id, content_text, content_type, image_path,
            ocr_text, content_search, source, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        count = 0
        with self._connect() as conn:
            for msg in messages:
                cursor = conn.execute(
                    insert_sql,
                    (
                        msg.group_id,
                        msg.message_id,
                        msg.sender,
                        msg.sender_id,
                        msg.sender_time,
                        msg.essence_time,
                        msg.operator,
                        msg.operator_id,
                        msg.content_text,
                        msg.content_type,
                        msg.image_path,
                        msg.ocr_text,
                        msg.normalized_content_for_search(),
                        msg.source,
                        json.dumps(msg.raw_data or {}, ensure_ascii=False),
                    ),
                )
                if cursor.rowcount > 0:
                    count += 1
        return count

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

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

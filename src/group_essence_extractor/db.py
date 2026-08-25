from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import EssenceMessage, MessageTimeRecord


SCHEMA_VERSION = 3

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

CREATE_ATTACHMENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS essence_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    essence_id INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    remote_url TEXT NOT NULL,
    local_path TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    byte_size INTEGER NOT NULL DEFAULT 0,
    ocr_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY(essence_id) REFERENCES essence_messages(id) ON DELETE CASCADE,
    UNIQUE(essence_id, position)
);
"""

CREATE_ATTACHMENTS_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_attachment_essence ON essence_attachments(essence_id);",
    "CREATE INDEX IF NOT EXISTS idx_attachment_hash ON essence_attachments(content_sha256);",
    "CREATE INDEX IF NOT EXISTS idx_attachment_status ON essence_attachments(status);",
]

CREATE_SYNC_STATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS essence_sync_state (
    group_id TEXT PRIMARY KEY,
    last_started_at TEXT NOT NULL DEFAULT '',
    last_finished_at TEXT NOT NULL DEFAULT '',
    last_success_at TEXT NOT NULL DEFAULT '',
    next_run_at TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error_category TEXT NOT NULL DEFAULT '',
    last_collected INTEGER NOT NULL DEFAULT 0,
    last_inserted INTEGER NOT NULL DEFAULT 0,
    last_updated INTEGER NOT NULL DEFAULT 0,
    last_refreshed INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    running INTEGER NOT NULL DEFAULT 0,
    alert_state TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""

CREATE_DETAIL_RETRY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS essence_detail_retry (
    group_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT NOT NULL DEFAULT '',
    last_error_category TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(group_id, message_id)
);
"""

CREATE_RUNTIME_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_sync_state_next_run ON essence_sync_state(next_run_at);",
    "CREATE INDEX IF NOT EXISTS idx_detail_retry_next ON essence_detail_retry(group_id, next_retry_at);",
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

PERSISTED_RAW_METADATA_KEYS = (
    "sender_time_source",
    "sender_time_repair",
)


@dataclass(frozen=True)
class SaveStats:
    inserted: int = 0
    updated: int = 0
    refreshed: int = 0
    unchanged: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "refreshed": self.refreshed,
            "unchanged": self.unchanged,
        }


@dataclass(frozen=True)
class SenderTimeBackfillStats:
    candidates: int = 0
    matched: int = 0
    updated: int = 0
    remaining: int = 0


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


@dataclass(frozen=True)
class SyncState:
    group_id: str
    last_started_at: str = ""
    last_finished_at: str = ""
    last_success_at: str = ""
    next_run_at: str = ""
    consecutive_failures: int = 0
    last_error_category: str = ""
    last_collected: int = 0
    last_inserted: int = 0
    last_updated: int = 0
    last_refreshed: int = 0
    duration_ms: int = 0
    running: bool = False
    alert_state: str = ""
    updated_at: str = ""


class EssenceRepository:
    def __init__(
        self,
        db_path: Path,
        *,
        backup_dir: Path | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.db_path = db_path
        self.backup_dir = backup_dir or (db_path.parent / "backups")
        self.busy_timeout_ms = max(0, min(int(busy_timeout_ms), 60_000))

    def _connect(self, read_only: bool = False) -> sqlite3.Connection:
        timeout_seconds = self.busy_timeout_ms / 1000
        if read_only:
            uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
        else:
            conn = sqlite3.connect(self.db_path, timeout=timeout_seconds)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return conn

    def init_db(self) -> MigrationStats:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        from_version = 0
        should_backup = self.db_path.is_file() and self.db_path.stat().st_size > 0
        if should_backup:
            with closing(self._connect(read_only=True)) as read_conn:
                from_version = int(
                    read_conn.execute("PRAGMA user_version").fetchone()[0]
                )
            if from_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {from_version} 高于程序支持的版本 {SCHEMA_VERSION}"
                )
            if from_version < SCHEMA_VERSION:
                self.create_backup(
                    reason=f"pre-migration-v{from_version}-to-v{SCHEMA_VERSION}",
                    managed=False,
                )

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

            if from_version < 2:
                conn.execute(CREATE_ATTACHMENTS_TABLE_SQL)
                for sql in CREATE_ATTACHMENTS_INDEX_SQL:
                    conn.execute(sql)
                conn.execute("PRAGMA user_version = 2")
                applied.append(2)

            if from_version < 3:
                conn.execute(CREATE_SYNC_STATE_TABLE_SQL)
                conn.execute(CREATE_DETAIL_RETRY_TABLE_SQL)
                for sql in CREATE_RUNTIME_INDEX_SQL:
                    conn.execute(sql)
                conn.execute("PRAGMA user_version = 3")
                applied.append(3)

            return MigrationStats(
                from_version=from_version,
                to_version=SCHEMA_VERSION,
                applied=tuple(applied),
            )

    def create_backup(
        self,
        *,
        reason: str = "scheduled",
        managed: bool = True,
        now: datetime | None = None,
    ) -> Path:
        """Create and verify an online SQLite snapshot without copying a live file."""
        if not self.db_path.is_file():
            raise FileNotFoundError(self.db_path)
        timestamp = _as_utc(now or datetime.now(timezone.utc))
        safe_reason = "scheduled" if managed else _safe_backup_reason(reason)
        filename = f"{safe_reason}-{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}.db"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.backup_dir, 0o700)
        except OSError:
            pass
        destination = self.backup_dir / filename
        temporary = destination.with_suffix(".db.tmp")
        try:
            with closing(self._connect(read_only=True)) as source, closing(
                sqlite3.connect(
                    temporary,
                    timeout=self.busy_timeout_ms / 1000,
                )
            ) as target:
                source.backup(target)
                integrity = str(target.execute("PRAGMA quick_check").fetchone()[0])
                if integrity != "ok":
                    raise sqlite3.DatabaseError("backup quick_check failed")
            os.replace(temporary, destination)
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
            return destination
        finally:
            if temporary.exists():
                temporary.unlink()

    def latest_managed_backup_at(self) -> str:
        backups = self._managed_backup_paths()
        if not backups:
            return ""
        latest = max(backups, key=lambda path: path.stat().st_mtime)
        timestamp = datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc)
        return _format_utc(timestamp)

    def prune_managed_backups(
        self,
        *,
        keep_daily: int,
        keep_weekly: int,
    ) -> tuple[str, ...]:
        backups = sorted(
            self._managed_backup_paths(),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        daily_limit = max(1, min(int(keep_daily), 31))
        weekly_limit = max(0, min(int(keep_weekly), 52))
        daily_keys: set[str] = set()
        weekly_keys: set[str] = set()
        retained: set[Path] = set()
        for path in backups:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            day_key = modified.strftime("%Y-%m-%d")
            iso_year, iso_week, _ = modified.isocalendar()
            week_key = f"{iso_year:04d}-{iso_week:02d}"
            if day_key not in daily_keys and len(daily_keys) < daily_limit:
                daily_keys.add(day_key)
                retained.add(path)
            if week_key not in weekly_keys and len(weekly_keys) < weekly_limit:
                weekly_keys.add(week_key)
                retained.add(path)

        removed: list[str] = []
        for path in backups:
            if path in retained:
                continue
            path.unlink()
            removed.append(path.name)
        return tuple(removed)

    def _managed_backup_paths(self) -> list[Path]:
        if not self.backup_dir.is_dir():
            return []
        return [
            path
            for path in self.backup_dir.glob("scheduled-*.db")
            if path.is_file()
        ]

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
                attachment_table_present = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'essence_attachments'"
                ).fetchone() is not None
                attachment_summary: dict[str, Any] = {
                    "table_present": attachment_table_present,
                    "total": 0,
                    "with_local_file": 0,
                    "with_ocr": 0,
                    "by_status": {},
                }
                if attachment_table_present:
                    attachment_summary.update(
                        {
                            "total": int(
                                conn.execute("SELECT COUNT(*) FROM essence_attachments").fetchone()[0]
                            ),
                            "with_local_file": int(
                                conn.execute(
                                    """
                                    SELECT COUNT(*) FROM essence_attachments
                                    WHERE TRIM(local_path) <> ''
                                    """
                                ).fetchone()[0]
                            ),
                            "with_ocr": int(
                                conn.execute(
                                    """
                                    SELECT COUNT(*) FROM essence_attachments
                                    WHERE TRIM(ocr_text) <> ''
                                    """
                                ).fetchone()[0]
                            ),
                            "by_status": {
                                str(row[0]): int(row[1])
                                for row in conn.execute(
                                    """
                                    SELECT COALESCE(status, ''), COUNT(*)
                                    FROM essence_attachments GROUP BY status
                                    """
                                ).fetchall()
                            },
                        }
                    )

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
                    "attachments": attachment_summary,
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
        refreshed = 0
        unchanged = 0
        with closing(self._connect()) as conn, conn:
            for msg in messages:
                values = self._message_values(msg)
                existing = self._find_existing(conn, msg)
                if existing is not None:
                    old_values = {
                        column: existing[column] if existing[column] is not None else ""
                        for column in MESSAGE_COLUMNS
                    }
                    merged_values = _merge_upsert_values(old_values, values)
                    changed_columns = {
                        column
                        for column in MESSAGE_COLUMNS
                        if old_values[column] != merged_values[column]
                    }
                    if not changed_columns:
                        unchanged += 1
                        continue

                    assignments = ", ".join(f"{column} = ?" for column in MESSAGE_COLUMNS)
                    conn.execute(
                        f"UPDATE essence_messages SET {assignments} WHERE id = ?",
                        (
                            *(merged_values[column] for column in MESSAGE_COLUMNS),
                            existing["id"],
                        ),
                    )
                    if _is_refresh_only(msg.source, changed_columns):
                        refreshed += 1
                    else:
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
        return SaveStats(
            inserted=inserted,
            updated=updated,
            refreshed=refreshed,
            unchanged=unchanged,
        )

    def insert_messages(self, messages: Iterable[EssenceMessage]) -> int:
        """兼容旧调用方，仅返回实际新增记录数。"""
        return self.upsert_messages(messages).inserted

    def unseen_message_ids(
        self,
        group_id: str,
        messages: Iterable[EssenceMessage],
    ) -> set[str]:
        """返回数据库尚未持久化的 OneBot 消息 ID，且不创建数据库。"""
        candidates = {
            message.message_id.strip()
            for message in messages
            if message.source == "onebot" and message.message_id.strip()
        }
        if not candidates or not self.db_path.is_file():
            return candidates
        with closing(self._connect(read_only=True)) as conn:
            rows = conn.execute(
                """
                SELECT message_id
                FROM essence_messages
                WHERE source = 'onebot' AND group_id = ?
                  AND message_id IS NOT NULL AND TRIM(message_id) <> ''
                """,
                (str(group_id or "").strip(),),
            ).fetchall()
        existing = {str(row[0]).strip() for row in rows if str(row[0] or "").strip()}
        return candidates - existing

    def blocked_detail_retry_ids(
        self,
        group_id: str,
        *,
        now: datetime | None = None,
    ) -> set[str]:
        """Return detail IDs whose retry deadline is still in the future."""
        if not self.db_path.is_file():
            return set()
        with closing(self._connect(read_only=True)) as conn:
            if not _table_exists(conn, "essence_detail_retry"):
                return set()
            rows = conn.execute(
                """
                SELECT message_id
                FROM essence_detail_retry
                WHERE group_id = ? AND next_retry_at > ?
                """,
                (
                    str(group_id or "").strip(),
                    _format_utc(_as_utc(now or datetime.now(timezone.utc))),
                ),
            ).fetchall()
        return {str(row["message_id"]).strip() for row in rows}

    def update_detail_retry_states(
        self,
        group_id: str,
        *,
        failed: dict[str, str],
        resolved: Iterable[str],
        base_minutes: int,
        max_hours: int,
        now: datetime | None = None,
    ) -> None:
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            raise ValueError("detail retry state requires group_id")
        timestamp = _as_utc(now or datetime.now(timezone.utc))
        now_text = _format_utc(timestamp)
        base_delay = max(1, min(int(base_minutes), 1440))
        max_delay = max(1, min(int(max_hours), 168)) * 60
        resolved_ids = {
            str(message_id).strip()
            for message_id in resolved
            if str(message_id).strip()
        }
        failed_ids = {
            str(message_id).strip(): str(category or "detail_failed")[:64]
            for message_id, category in failed.items()
            if str(message_id).strip()
        }
        resolved_ids.difference_update(failed_ids)
        with closing(self._connect()) as conn, conn:
            for message_id in sorted(resolved_ids):
                conn.execute(
                    "DELETE FROM essence_detail_retry WHERE group_id = ? AND message_id = ?",
                    (normalized_group_id, message_id),
                )
            for message_id, category in sorted(failed_ids.items()):
                row = conn.execute(
                    """
                    SELECT failure_count FROM essence_detail_retry
                    WHERE group_id = ? AND message_id = ?
                    """,
                    (normalized_group_id, message_id),
                ).fetchone()
                failure_count = int(row[0] if row else 0) + 1
                exponent = min(max(0, failure_count - 1), 20)
                delay_minutes = min(base_delay * (2**exponent), max_delay)
                next_retry_at = _format_utc(
                    timestamp + timedelta(minutes=delay_minutes)
                )
                conn.execute(
                    """
                    INSERT INTO essence_detail_retry (
                        group_id, message_id, failure_count, next_retry_at,
                        last_error_category, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(group_id, message_id) DO UPDATE SET
                        failure_count = excluded.failure_count,
                        next_retry_at = excluded.next_retry_at,
                        last_error_category = excluded.last_error_category,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized_group_id,
                        message_id,
                        failure_count,
                        next_retry_at,
                        category,
                        now_text,
                    ),
                )

    def get_sync_state(self, group_id: str) -> SyncState:
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            raise ValueError("sync state requires group_id")
        if not self.db_path.is_file():
            return SyncState(group_id=normalized_group_id)
        with closing(self._connect(read_only=True)) as conn:
            if not _table_exists(conn, "essence_sync_state"):
                return SyncState(group_id=normalized_group_id)
            row = conn.execute(
                "SELECT * FROM essence_sync_state WHERE group_id = ?",
                (normalized_group_id,),
            ).fetchone()
        return _sync_state_from_row(row, normalized_group_id)

    def list_sync_states(self, group_ids: Iterable[str]) -> list[SyncState]:
        normalized_ids = sorted(
            {
                str(group_id).strip()
                for group_id in group_ids
                if str(group_id).strip()
            }
        )
        if not normalized_ids:
            return []
        if not self.db_path.is_file():
            return [SyncState(group_id=group_id) for group_id in normalized_ids]
        with closing(self._connect(read_only=True)) as conn:
            if not _table_exists(conn, "essence_sync_state"):
                return [SyncState(group_id=group_id) for group_id in normalized_ids]
            placeholders = ", ".join("?" for _ in normalized_ids)
            rows = conn.execute(
                f"SELECT * FROM essence_sync_state WHERE group_id IN ({placeholders})",
                normalized_ids,
            ).fetchall()
        by_group = {
            str(row["group_id"]): _sync_state_from_row(row, str(row["group_id"]))
            for row in rows
        }
        return [by_group.get(group_id, SyncState(group_id)) for group_id in normalized_ids]

    def save_sync_state(self, state: SyncState) -> None:
        normalized_group_id = str(state.group_id or "").strip()
        if not normalized_group_id:
            raise ValueError("sync state requires group_id")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO essence_sync_state (
                    group_id, last_started_at, last_finished_at, last_success_at,
                    next_run_at, consecutive_failures, last_error_category,
                    last_collected, last_inserted, last_updated, last_refreshed,
                    duration_ms, running, alert_state, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    last_started_at = excluded.last_started_at,
                    last_finished_at = excluded.last_finished_at,
                    last_success_at = excluded.last_success_at,
                    next_run_at = excluded.next_run_at,
                    consecutive_failures = excluded.consecutive_failures,
                    last_error_category = excluded.last_error_category,
                    last_collected = excluded.last_collected,
                    last_inserted = excluded.last_inserted,
                    last_updated = excluded.last_updated,
                    last_refreshed = excluded.last_refreshed,
                    duration_ms = excluded.duration_ms,
                    running = excluded.running,
                    alert_state = excluded.alert_state,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_group_id,
                    state.last_started_at,
                    state.last_finished_at,
                    state.last_success_at,
                    state.next_run_at,
                    max(0, int(state.consecutive_failures)),
                    str(state.last_error_category or "")[:64],
                    max(0, int(state.last_collected)),
                    max(0, int(state.last_inserted)),
                    max(0, int(state.last_updated)),
                    max(0, int(state.last_refreshed)),
                    max(0, int(state.duration_ms)),
                    int(bool(state.running)),
                    str(state.alert_state or "")[:32],
                    state.updated_at or _format_utc(datetime.now(timezone.utc)),
                ),
            )

    def clear_stale_running_states(self) -> int:
        if not self.db_path.is_file():
            return 0
        with closing(self._connect()) as conn, conn:
            if not _table_exists(conn, "essence_sync_state"):
                return 0
            cursor = conn.execute(
                """
                UPDATE essence_sync_state
                SET running = 0, updated_at = ?
                WHERE running <> 0
                """,
                (_format_utc(datetime.now(timezone.utc)),),
            )
        return max(0, cursor.rowcount)

    def backfill_sender_times(
        self,
        group_id: str,
        history: Iterable[MessageTimeRecord],
    ) -> SenderTimeBackfillStats:
        """只补空缺发送时间，并以脱敏元数据记录来源。"""
        if not self.db_path.is_file():
            return SenderTimeBackfillStats()
        by_id, by_sequence_random, by_sequence = _history_record_indexes(history)
        normalized_group_id = str(group_id or "").strip()
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                """
                SELECT id, message_id, raw_json
                FROM essence_messages
                WHERE source = 'onebot' AND group_id = ?
                  AND (sender_time IS NULL OR TRIM(sender_time) = '')
                ORDER BY id
                """,
                (normalized_group_id,),
            ).fetchall()
            matched = 0
            updated = 0
            for row in rows:
                raw = _load_raw_json(row["raw_json"])
                essence = raw.get("essence")
                if not isinstance(essence, dict):
                    essence = {}
                sequence = _mapping_identity(
                    essence,
                    "msg_seq",
                    "message_seq",
                    "real_seq",
                    "seq",
                )
                random_value = _mapping_identity(
                    essence,
                    "msg_random",
                    "message_random",
                    "random",
                )
                message_id = str(row["message_id"] or "").strip()
                record = by_id.get(message_id)
                if record is None and sequence and random_value:
                    record = by_sequence_random.get(f"{sequence}:{random_value}")
                if record is None and sequence:
                    record = by_sequence.get(sequence)
                if record is None:
                    continue

                matched += 1
                raw["sender_time_source"] = "group_history"
                raw["sender_time_repair"] = {"source": "group_history"}
                cursor = conn.execute(
                    """
                    UPDATE essence_messages
                    SET sender_time = ?, raw_json = ?
                    WHERE id = ? AND (sender_time IS NULL OR TRIM(sender_time) = '')
                    """,
                    (
                        record.sender_time,
                        json.dumps(raw, ensure_ascii=False, sort_keys=True),
                        int(row["id"]),
                    ),
                )
                updated += max(0, cursor.rowcount)
        candidates = len(rows)
        return SenderTimeBackfillStats(
            candidates=candidates,
            matched=matched,
            updated=updated,
            remaining=max(0, candidates - updated),
        )

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

    def list_image_messages(self, group_id: str = "") -> list[dict[str, Any]]:
        """只读返回包含 OneBot 图片地址的消息，不创建或迁移数据库。"""
        conditions = ["source = 'onebot'", "TRIM(COALESCE(image_path, '')) <> ''"]
        params: list[str] = []
        if group_id.strip():
            conditions.append("group_id = ?")
            params.append(group_id.strip())
        with closing(self._connect(read_only=True)) as conn:
            rows = conn.execute(
                f"""
                SELECT id, group_id, message_id, content_text, image_path
                FROM essence_messages
                WHERE {' AND '.join(conditions)}
                ORDER BY id
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_image_attachments(
        self,
        essence_ids: Iterable[int],
    ) -> list[dict[str, Any]]:
        """只读返回已有附件；旧 schema 尚无附件表时返回空列表。"""
        ids = sorted({int(value) for value in essence_ids})
        if not ids:
            return []
        with closing(self._connect(read_only=True)) as conn:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'essence_attachments'"
            ).fetchone()
            if table_exists is None:
                return []
            placeholders = ", ".join("?" for _ in ids)
            rows = conn.execute(
                f"""
                SELECT id, essence_id, position, remote_url, local_path,
                       content_sha256, mime_type, byte_size, ocr_text, status, error,
                       created_at, updated_at
                FROM essence_attachments
                WHERE essence_id IN ({placeholders})
                ORDER BY essence_id, position, id
                """,
                ids,
            ).fetchall()
        return [dict(row) for row in rows]

    def save_image_attachment(
        self,
        *,
        essence_id: int,
        position: int,
        remote_url: str,
        local_path: str,
        content_sha256: str,
        mime_type: str,
        byte_size: int,
        ocr_text: str,
        status: str,
        error: str = "",
    ) -> None:
        """保存单个附件结果，并刷新所属消息的聚合 OCR 搜索文本。"""
        if status not in {"completed", "no_text", "failed"}:
            raise ValueError(f"不支持的附件状态: {status}")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO essence_attachments (
                    essence_id, position, remote_url, local_path, content_sha256,
                    mime_type, byte_size, ocr_text, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(essence_id, position) DO UPDATE SET
                    remote_url = excluded.remote_url,
                    local_path = excluded.local_path,
                    content_sha256 = excluded.content_sha256,
                    mime_type = excluded.mime_type,
                    byte_size = excluded.byte_size,
                    ocr_text = excluded.ocr_text,
                    status = excluded.status,
                    error = excluded.error,
                    updated_at = datetime('now', 'localtime')
                """,
                (
                    int(essence_id),
                    max(0, int(position)),
                    remote_url,
                    local_path,
                    content_sha256,
                    mime_type,
                    max(0, int(byte_size)),
                    ocr_text,
                    status,
                    error,
                ),
            )
            message = conn.execute(
                "SELECT content_text FROM essence_messages WHERE id = ?",
                (int(essence_id),),
            ).fetchone()
            if message is None:
                raise ValueError(f"附件所属消息不存在: {essence_id}")
            ocr_rows = conn.execute(
                """
                SELECT ocr_text FROM essence_attachments
                WHERE essence_id = ? AND status = 'completed' AND TRIM(ocr_text) <> ''
                ORDER BY position, id
                """,
                (int(essence_id),),
            ).fetchall()
            ocr_parts: list[str] = []
            for row in ocr_rows:
                text = str(row[0]).strip()
                if text and text not in ocr_parts:
                    ocr_parts.append(text)
            aggregated_ocr = "\n".join(ocr_parts)
            conn.execute(
                """
                UPDATE essence_messages
                SET ocr_text = ?, content_search = ?
                WHERE id = ?
                """,
                (
                    aggregated_ocr,
                    _normalized_search_text(message["content_text"], aggregated_ocr),
                    int(essence_id),
                ),
            )

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


def _merge_upsert_values(
    old_values: dict[str, str],
    new_values: dict[str, str],
) -> dict[str, str]:
    """合并同步结果，避免上游空值抹掉已修复字段或 OCR 数据。"""
    merged = dict(new_values)
    for column in (
        "group_id",
        "message_id",
        "sender_id",
        "sender_time",
        "essence_time",
        "operator_id",
        "image_path",
        "ocr_text",
    ):
        if _is_blank(merged[column]) and not _is_blank(old_values[column]):
            merged[column] = old_values[column]

    if merged["sender"] in {"", "未知发送者"} and old_values["sender"]:
        merged["sender"] = old_values["sender"]
    if merged["operator"] in {"", "未知设置人"} and old_values["operator"]:
        merged["operator"] = old_values["operator"]

    incoming_content_missing = merged["content_text"] in {"", "[空消息]"}
    if incoming_content_missing and old_values["content_text"] not in {"", "[空消息]"}:
        for column in ("content_text", "content_type", "image_path"):
            merged[column] = old_values[column]

    merged["content_search"] = _normalized_search_text(
        merged["content_text"],
        merged["ocr_text"],
    )
    merged["raw_json"] = _merge_raw_json(
        old_values["raw_json"],
        merged["raw_json"],
        preserve_detail_failure=incoming_content_missing,
    )
    return merged


def _merge_raw_json(
    old_value: str,
    new_value: str,
    *,
    preserve_detail_failure: bool,
) -> str:
    old_raw = _load_raw_json(old_value)
    new_raw = _load_raw_json(new_value)
    if not new_raw:
        return old_value if old_raw else new_value
    for key in PERSISTED_RAW_METADATA_KEYS:
        if key not in new_raw and key in old_raw:
            new_raw[key] = old_raw[key]
    if preserve_detail_failure:
        for key in ("message_detail_error", "message_detail_requested"):
            if key not in new_raw and key in old_raw:
                new_raw[key] = old_raw[key]
    return json.dumps(new_raw, ensure_ascii=False, sort_keys=True)


def _is_refresh_only(source: str, changed_columns: set[str]) -> bool:
    refresh_columns = {"raw_json"}
    if source == "onebot":
        refresh_columns.add("image_path")
    return bool(changed_columns) and changed_columns.issubset(refresh_columns)


def _history_record_indexes(
    history: Iterable[MessageTimeRecord],
) -> tuple[
    dict[str, MessageTimeRecord],
    dict[str, MessageTimeRecord],
    dict[str, MessageTimeRecord],
]:
    records = [record for record in history if record.sender_time.strip()]
    return (
        _unique_history_index(records, lambda record: record.message_id),
        _unique_history_index(
            records,
            lambda record: (
                f"{record.message_seq}:{record.message_random}"
                if record.message_seq and record.message_random
                else ""
            ),
        ),
        _unique_history_index(records, lambda record: record.message_seq),
    )


def _unique_history_index(
    records: Iterable[MessageTimeRecord],
    key_builder: Callable[[MessageTimeRecord], str],
) -> dict[str, MessageTimeRecord]:
    index: dict[str, MessageTimeRecord] = {}
    ambiguous: set[str] = set()
    for record in records:
        key = key_builder(record)
        if not key or key in ambiguous:
            continue
        if key in index and index[key] != record:
            index.pop(key, None)
            ambiguous.add(key)
            continue
        index[key] = record
    return index


def _mapping_identity(value: dict[str, Any], *keys: str) -> str:
    for key in keys:
        item = value.get(key)
        if item is not None and str(item).strip():
            return str(item).strip()
    return ""


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


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _sync_state_from_row(row: sqlite3.Row | None, group_id: str) -> SyncState:
    if row is None:
        return SyncState(group_id=group_id)
    return SyncState(
        group_id=group_id,
        last_started_at=str(row["last_started_at"] or ""),
        last_finished_at=str(row["last_finished_at"] or ""),
        last_success_at=str(row["last_success_at"] or ""),
        next_run_at=str(row["next_run_at"] or ""),
        consecutive_failures=max(0, int(row["consecutive_failures"] or 0)),
        last_error_category=str(row["last_error_category"] or ""),
        last_collected=max(0, int(row["last_collected"] or 0)),
        last_inserted=max(0, int(row["last_inserted"] or 0)),
        last_updated=max(0, int(row["last_updated"] or 0)),
        last_refreshed=max(0, int(row["last_refreshed"] or 0)),
        duration_ms=max(0, int(row["duration_ms"] or 0)),
        running=bool(row["running"]),
        alert_state=str(row["alert_state"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return _as_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _safe_backup_reason(value: str) -> str:
    normalized = "".join(
        character
        for character in str(value or "").lower()
        if character.isalnum() or character in {"-", "_"}
    )
    return normalized[:80] or "manual"

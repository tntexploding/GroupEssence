from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from group_essence_extractor.db import (
    CREATE_TABLE_SQL,
    SCHEMA_VERSION,
    EssenceRepository,
    MigrationStats,
    SaveStats,
)
from group_essence_extractor.models import EssenceMessage


def make_message(**overrides: str) -> EssenceMessage:
    values = {
        "sender": "发送者",
        "sender_id": "10001",
        "sender_time": "2026-05-01 10:00:00",
        "essence_time": "2026-05-01 10:05:00",
        "operator": "管理员",
        "operator_id": "10002",
        "content_text": "第一条消息",
        "content_type": "text",
        "group_id": "123456",
        "message_id": "message-1",
        "source": "onebot",
    }
    values.update(overrides)
    return EssenceMessage(**values, raw_data={"message_id": values["message_id"]})


class EssenceRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.repo = EssenceRepository(self.db_path)
        self.repo.init_db()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_upsert_reports_insert_update_and_unchanged(self) -> None:
        original = make_message()
        self.assertEqual(self.repo.upsert_messages([original]), SaveStats(inserted=1))
        self.assertEqual(self.repo.upsert_messages([original]), SaveStats(unchanged=1))

        changed = make_message(
            sender_time="2026-05-01 09:59:59",
            content_text="更新后的活动通知",
        )
        self.assertEqual(self.repo.upsert_messages([changed]), SaveStats(updated=1))

        rows = self.repo.search(content="活动", sender_qq="10001")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sender_time"], "2026-05-01 09:59:59")
        self.assertEqual(rows[0]["content_text"], "更新后的活动通知")

    def test_schema_migration_is_versioned_and_idempotent(self) -> None:
        self.assertEqual(
            self.repo.init_db(),
            MigrationStats(from_version=SCHEMA_VERSION, to_version=SCHEMA_VERSION),
        )

        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as conn, conn:
            conn.execute(CREATE_TABLE_SQL)
            conn.execute(
                """
                INSERT INTO essence_messages (
                    sender, sender_time, essence_time, operator, content_text,
                    content_type, content_search, source
                ) VALUES ('旧用户', '', '', '旧管理员', '旧消息', 'text', '旧消息', 'onebot')
                """
            )
        migration = EssenceRepository(legacy_path).init_db()

        self.assertEqual(
            migration,
            MigrationStats(from_version=0, to_version=2, applied=(1, 2)),
        )
        with closing(sqlite3.connect(legacy_path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM essence_messages").fetchone()[0], 1)
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'essence_attachments'"
                ).fetchone()
            )

    def test_schema_v1_upgrades_to_attachment_table_without_losing_messages(self) -> None:
        version_one_path = Path(self.temp_dir.name) / "version-one.db"
        with closing(sqlite3.connect(version_one_path)) as conn, conn:
            conn.execute(CREATE_TABLE_SQL)
            conn.execute(
                """
                INSERT INTO essence_messages (
                    sender, sender_time, essence_time, operator, content_text,
                    content_type, content_search, source
                ) VALUES ('旧用户', '', '', '旧管理员', '图片消息', 'image', '图片消息', 'onebot')
                """
            )
            conn.execute("PRAGMA user_version = 1")

        migration = EssenceRepository(version_one_path).init_db()

        self.assertEqual(migration, MigrationStats(from_version=1, to_version=2, applied=(2,)))
        with closing(sqlite3.connect(version_one_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM essence_messages").fetchone()[0], 1)
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'essence_attachments'"
                ).fetchone()
            )

    def test_rejects_database_newer_than_supported_schema(self) -> None:
        future_path = Path(self.temp_dir.name) / "future.db"
        with closing(sqlite3.connect(future_path)) as conn, conn:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

        with self.assertRaisesRegex(RuntimeError, "高于程序支持"):
            EssenceRepository(future_path).init_db()

    def test_backfills_group_id_on_legacy_onebot_record(self) -> None:
        legacy = make_message(group_id="", sender_time="")
        self.assertEqual(self.repo.insert_messages([legacy]), 1)

        repaired = make_message(group_id="123456", sender_time="2026-05-01 10:00:00")
        self.assertEqual(self.repo.upsert_messages([repaired]), SaveStats(updated=1))

        rows = self.repo.search(sender_qq="10001")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["group_id"], "123456")
        self.assertEqual(rows[0]["sender_time"], "2026-05-01 10:00:00")

    def test_ocr_record_matches_legacy_image_path(self) -> None:
        legacy = make_message(
            source="ocr_screenshot",
            group_id="",
            message_id="",
            image_path="C:/screenshots/a.png",
            ocr_text="OCR 正文",
        )
        self.repo.upsert_messages([legacy])

        repaired = make_message(
            source="ocr_screenshot",
            group_id="123456",
            message_id="ocr:abc",
            image_path="C:/screenshots/a.png",
            ocr_text="OCR 正文",
        )
        self.assertEqual(self.repo.upsert_messages([repaired]), SaveStats(updated=1))

        rows = self.repo.search(sender="发送者")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], "ocr:abc")

    def test_search_clamps_pagination(self) -> None:
        self.repo.upsert_messages([make_message()])
        self.assertEqual(len(self.repo.search(limit=0, offset=-10)), 1)

    def test_search_page_supports_exact_ranges_and_total(self) -> None:
        self.repo.upsert_messages(
            [
                make_message(message_id="1", content_type="text"),
                make_message(
                    message_id="2",
                    sender_time="2026-05-02 10:00:00",
                    essence_time="2026-05-02 10:05:00",
                    content_type="mixed",
                ),
                make_message(
                    message_id="3",
                    group_id="other",
                    sender_time="2026-05-03 10:00:00",
                    essence_time="2026-05-03 10:05:00",
                ),
            ]
        )

        page = self.repo.search_page(
            group_id="123456",
            source="onebot",
            sender_time_from="2026-05-01 00:00:00",
            sender_time_to="2026-05-02 23:59:59",
            limit=1,
        )

        self.assertEqual(page.total, 2)
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0]["message_id"], "2")
        self.assertEqual(
            self.repo.search(content_type="mixed", group_id="123456")[0]["message_id"],
            "2",
        )

    def test_repair_previews_read_only_then_applies_recoverable_fields(self) -> None:
        legacy = make_message(
            group_id="",
            message_id="",
            sender_time="",
            essence_time="",
            content_text="修复正文",
        )
        legacy.raw_data = {
            "essence": {
                "group_id": "123456",
                "message_id": "legacy-1",
                "operator_time": "2026-05-01 10:05:00",
            },
            "message_detail": {"time": "2026-05-01 10:00:00"},
        }
        self.repo.upsert_messages([legacy])
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE essence_messages SET content_search = '过期索引'")
        modified_before = self.db_path.stat().st_mtime_ns

        preview = self.repo.repair()

        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["would_update"], 1)
        self.assertEqual(preview["candidates"]["sender_time"], 1)
        self.assertEqual(preview["candidates"]["content_search"], 1)
        self.assertEqual(preview["updated"], 0)
        self.assertEqual(self.db_path.stat().st_mtime_ns, modified_before)

        applied = self.repo.repair(apply=True)
        row = self.repo.search(content="修复正文")
        self.assertEqual(applied["updated"], 1)
        self.assertEqual(row[0]["group_id"], "123456")
        self.assertEqual(row[0]["message_id"], "legacy-1")
        self.assertEqual(row[0]["sender_time"], "2026-05-01 10:00:00")
        self.assertEqual(row[0]["essence_time"], "2026-05-01 10:05:00")

    def test_audit_is_read_only_and_reports_quality(self) -> None:
        self.repo.upsert_messages([make_message(), make_message(message_id="message-2", group_id="")])
        modified_before = self.db_path.stat().st_mtime_ns

        report = self.repo.audit()

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["integrity"], "ok")
        self.assertEqual(report["schema_version"], SCHEMA_VERSION)
        self.assertFalse(report["migration_required"])
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["by_source"], {"onebot": 2})
        self.assertEqual(report["missing"]["group_id"], 1)
        self.assertEqual(report["duplicates"]["message_identity"], 0)
        self.assertTrue(report["attachments"]["table_present"])
        self.assertEqual(report["attachments"]["total"], 0)
        self.assertEqual(self.db_path.stat().st_mtime_ns, modified_before)

    def test_audit_missing_database_does_not_create_paths(self) -> None:
        missing_path = Path(self.temp_dir.name) / "missing" / "database.db"
        report = EssenceRepository(missing_path).audit()

        self.assertEqual(report["status"], "error")
        self.assertFalse(missing_path.exists())
        self.assertFalse(missing_path.parent.exists())


if __name__ == "__main__":
    unittest.main()

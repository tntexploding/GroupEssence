from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from group_essence_extractor.db import EssenceRepository, SaveStats
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

    def test_audit_is_read_only_and_reports_quality(self) -> None:
        self.repo.upsert_messages([make_message(), make_message(message_id="message-2", group_id="")])
        modified_before = self.db_path.stat().st_mtime_ns

        report = self.repo.audit()

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["integrity"], "ok")
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["by_source"], {"onebot": 2})
        self.assertEqual(report["missing"]["group_id"], 1)
        self.assertEqual(report["duplicates"]["message_identity"], 0)
        self.assertEqual(self.db_path.stat().st_mtime_ns, modified_before)

    def test_audit_missing_database_does_not_create_paths(self) -> None:
        missing_path = Path(self.temp_dir.name) / "missing" / "database.db"
        report = EssenceRepository(missing_path).audit()

        self.assertEqual(report["status"], "error")
        self.assertFalse(missing_path.exists())
        self.assertFalse(missing_path.parent.exists())


if __name__ == "__main__":
    unittest.main()

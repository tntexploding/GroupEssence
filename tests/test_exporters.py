from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest

from group_essence_extractor.db import EssenceRepository
from group_essence_extractor.exporters import export_records
from group_essence_extractor.models import EssenceMessage


def make_message(message_id: str, content: str, group_id: str = "123456") -> EssenceMessage:
    return EssenceMessage(
        sender="测试用户",
        sender_id="10001",
        sender_time="2026-05-01 10:00:00",
        essence_time="2026-05-01 10:05:00",
        operator="管理员",
        operator_id="10002",
        content_text=content,
        group_id=group_id,
        message_id=message_id,
        source="onebot",
    )


class ExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = EssenceRepository(self.root / "export.db")
        self.repo.init_db()
        self.repo.upsert_messages(
            [make_message("1", "活动通知"), make_message("2", "日常记录", group_id="other")]
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_exports_filtered_json_with_max_records(self) -> None:
        output = self.root / "nested" / "records.json"

        report = export_records(
            self.repo,
            output,
            "json",
            filters={"source": "onebot"},
            max_records=1,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(report["total"], 2)
        self.assertEqual(report["exported"], 1)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(len(payload["items"]), 1)

    def test_exports_csv_and_refuses_implicit_overwrite(self) -> None:
        output = self.root / "records.csv"
        report = export_records(
            self.repo,
            output,
            "csv",
            filters={"group_id": "123456", "content": "活动"},
        )

        with output.open("r", encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(report["exported"], 1)
        self.assertEqual(rows[0]["content_text"], "活动通知")
        with self.assertRaises(FileExistsError):
            export_records(self.repo, output, "csv")

    def test_refuses_database_as_export_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能覆盖当前数据库"):
            export_records(self.repo, self.repo.db_path, "json", force=True)


if __name__ == "__main__":
    unittest.main()

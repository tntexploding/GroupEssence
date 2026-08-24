from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from group_essence_extractor.config import Settings
from group_essence_extractor.db import EssenceRepository
from group_essence_extractor.ingest import (
    ingest_all,
    ingest_from_screenshots,
    summarize_messages,
)
from group_essence_extractor.models import EssenceMessage
from group_essence_extractor.ocr import OCRResult
from group_essence_extractor.parsers import parse_screenshot_to_essence


def make_settings(root: Path) -> Settings:
    return Settings(
        db_path=root / "database.db",
        onebot_base_url="http://onebot.test",
        onebot_access_token="token",
        group_id="123456",
        prefer_onebot=True,
        fallback_ocr=True,
        ocr_lang="chi_sim+eng",
        tesseract_cmd="",
        screenshot_dir=root / "screenshots",
        image_dir=root / "images",
    )


def make_message(source: str = "onebot") -> EssenceMessage:
    return EssenceMessage(
        sender="发送者",
        sender_id="10001",
        sender_time="2026-05-01 10:00:00",
        essence_time="2026-05-01 10:05:00",
        operator="管理员",
        operator_id="10002",
        content_text="活动通知",
        group_id="123456",
        message_id="message-1" if source == "onebot" else "ocr:abc",
        image_path="sample.png" if source == "ocr_screenshot" else "",
        source=source,
    )


class IngestTests(unittest.TestCase):
    def test_dry_run_collects_quality_without_creating_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = make_settings(root)
            client = MagicMock()
            client.__enter__.return_value = client
            client.__exit__.return_value = None
            client.get_essence_messages.return_value = [make_message()]

            with patch("group_essence_extractor.ingest.OneBotClient", return_value=client):
                stats = ingest_all(settings, dry_run=True)

            self.assertTrue(stats["dry_run"])
            self.assertEqual(stats["collected"], 1)
            self.assertEqual(stats["quality"]["total"], 1)
            self.assertEqual(stats["quality"]["missing"]["sender_time"], 0)
            self.assertEqual(stats["inserted"], 0)
            self.assertFalse(settings.db_path.exists())

    def test_onebot_result_skips_ocr_and_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = make_settings(root)
            repo = EssenceRepository(settings.db_path)
            repo.init_db()
            client = MagicMock()
            client.__enter__.return_value = client
            client.__exit__.return_value = None
            client.get_essence_messages.return_value = [make_message()]

            with patch("group_essence_extractor.ingest.OneBotClient", return_value=client), patch(
                "group_essence_extractor.ingest.ingest_from_screenshots"
            ) as ocr_fallback:
                stats = ingest_all(settings, repo)

            ocr_fallback.assert_not_called()
            self.assertEqual(stats["from_onebot"], 1)
            self.assertEqual(stats["inserted"], 1)
            self.assertEqual(stats["updated"], 0)

    def test_onebot_failure_uses_ocr_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = make_settings(root)
            repo = EssenceRepository(settings.db_path)
            repo.init_db()
            client = MagicMock()
            client.__enter__.return_value = client
            client.__exit__.return_value = None
            client.get_essence_messages.side_effect = RuntimeError("OneBot unavailable")

            with patch("group_essence_extractor.ingest.OneBotClient", return_value=client), patch(
                "group_essence_extractor.ingest.ingest_from_screenshots",
                return_value=([make_message("ocr_screenshot")], 1),
            ) as ocr_fallback:
                stats = ingest_all(settings, repo)

            ocr_fallback.assert_called_once_with(
                settings.screenshot_dir,
                settings.ocr_lang,
                settings.tesseract_cmd,
                settings.group_id,
            )
            self.assertIn("OneBot unavailable", stats["onebot_error"])
            self.assertEqual(stats["from_ocr"], 1)
            self.assertEqual(stats["ocr_error_count"], 1)
            self.assertEqual(stats["inserted"], 1)

    def test_screenshot_scan_counts_individual_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            screenshot_dir = Path(temp)
            (screenshot_dir / "a.png").write_bytes(b"a")
            (screenshot_dir / "b.jpg").write_bytes(b"b")
            (screenshot_dir / "ignored.txt").write_text("ignored", encoding="utf-8")

            def parse(image_path: Path, **_: str) -> EssenceMessage:
                if image_path.name == "b.jpg":
                    raise RuntimeError("bad image")
                return make_message("ocr_screenshot")

            with patch(
                "group_essence_extractor.ingest.parse_screenshot_to_essence",
                side_effect=parse,
            ) as parser:
                messages, error_count = ingest_from_screenshots(
                    screenshot_dir,
                    "chi_sim+eng",
                    "",
                    "123456",
                )

            self.assertEqual(len(messages), 1)
            self.assertEqual(error_count, 1)
            self.assertEqual(parser.call_count, 2)

    def test_screenshot_parser_uses_content_hash_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image_path = Path(temp) / "sample.png"
            image_path.write_bytes(b"stable-image-content")
            ocr_text = (
                "发送者：测试用户\n发送时间：2026-05-01 10:00:00\n"
                "精华时间：2026-05-01 10:05:00\n设置人：管理员\n正文"
            )
            with patch(
                "group_essence_extractor.parsers.image_to_result",
                return_value=OCRResult(ocr_text, 88.5, 9, "original"),
            ):
                message = parse_screenshot_to_essence(
                    image_path,
                    "chi_sim+eng",
                    "",
                    group_id="123456",
                )

            self.assertEqual(message.group_id, "123456")
            self.assertTrue(message.message_id.startswith("ocr:"))
            self.assertEqual(len(message.message_id), 68)
            self.assertEqual(message.sender, "测试用户")
            self.assertEqual(message.content_text, "正文")
            self.assertEqual(message.raw_data["parser_profile"], "labeled")
            self.assertEqual(message.raw_data["ocr_profile"], "original")
            self.assertEqual(message.normalized_content_for_search(), f"正文\n{ocr_text}")

    def test_screenshot_parser_understands_qq_essence_card_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image_path = Path(temp) / "card.png"
            image_path.write_bytes(b"qq-card")
            ocr_text = (
                "测 试 用 户\n"
                "2026 年 5 月 1 日 · 2026 / 5 / 2 "
                "由 管 理 员 设置为精华\n"
                "活 动 通 知"
            )
            with patch(
                "group_essence_extractor.parsers.image_to_result",
                return_value=OCRResult(ocr_text, 76.25, 15, "original"),
            ):
                message = parse_screenshot_to_essence(
                    image_path,
                    "chi_sim+eng",
                    "",
                    group_id="123456",
                )

            self.assertEqual(message.sender, "测试用户")
            self.assertEqual(message.sender_time, "2026-05-01")
            self.assertEqual(message.essence_time, "2026-05-02")
            self.assertEqual(message.operator, "管理员")
            self.assertEqual(message.content_text, "活动通知")
            self.assertEqual(message.raw_data["parser_profile"], "qq_essence_card")

    def test_quality_counts_unknown_ocr_placeholders_as_missing(self) -> None:
        message = EssenceMessage(
            sender="未知发送者",
            sender_time="",
            essence_time="",
            operator="未知设置人",
            content_text="识别到的正文",
            image_path="card.png",
            ocr_text="识别到的正文",
            source="ocr_screenshot",
            raw_data={
                "parser_profile": "fallback",
                "ocr_profile": "scale3_gray",
                "ocr_mean_confidence": 42.5,
            },
        )

        quality = summarize_messages([message])

        self.assertEqual(quality["missing"]["sender"], 1)
        self.assertEqual(quality["missing"]["operator"], 1)
        self.assertEqual(quality["ocr_quality"]["structured_complete"], 0)
        self.assertEqual(quality["ocr_quality"]["mean_confidence"], 42.5)
        self.assertEqual(
            quality["ocr_quality"]["by_recognition_profile"],
            {"scale3_gray": 1},
        )


if __name__ == "__main__":
    unittest.main()

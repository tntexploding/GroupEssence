from __future__ import annotations

from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from PIL import Image
import requests

from group_essence_extractor.db import CREATE_TABLE_SQL, EssenceRepository
from group_essence_extractor.image_enrichment import enrich_images
from group_essence_extractor.models import EssenceMessage


def make_png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color=(32, 128, 224)).save(buffer, format="PNG")
    return buffer.getvalue()


class ImageHandler(BaseHTTPRequestHandler):
    payload = make_png()
    requests: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        type(self).requests.append(self.path)
        if self.path == "/missing.png":
            self.send_error(404)
            return
        if self.path not in {"/a.png", "/b.png"}:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(type(self).payload)))
        self.end_headers()
        self.wfile.write(type(self).payload)

    def log_message(self, _: str, *args: object) -> None:
        return None


def make_message(message_id: str, image_path: str) -> EssenceMessage:
    return EssenceMessage(
        sender="测试用户",
        sender_time="2026-05-01 10:00:00",
        essence_time="2026-05-01 10:05:00",
        operator="管理员",
        content_text="原始正文",
        content_type="image",
        image_path=image_path,
        group_id="123456",
        message_id=message_id,
        source="onebot",
    )


class ImageEnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = EssenceRepository(self.root / "images.db")
        self.repo.init_db()
        ImageHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ImageHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.session = requests.Session()
        self.session.trust_env = False

    def tearDown(self) -> None:
        self.session.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def test_preview_is_read_only_offline_and_does_not_create_cache(self) -> None:
        self.repo.upsert_messages(
            [make_message("1", f"{self.base_url}/a.png\nfile:///not-remote.png")]
        )
        modified_before = self.repo.db_path.stat().st_mtime_ns

        report = enrich_images(
            self.repo,
            self.root / "cache",
            "chi_sim+eng",
            apply=False,
            session=self.session,
        )

        self.assertTrue(report["dry_run"])
        self.assertEqual(report["discovered"], 1)
        self.assertEqual(report["unsupported"], 1)
        self.assertEqual(report["pending"], 1)
        self.assertEqual(report["processed"], 0)
        self.assertEqual(ImageHandler.requests, [])
        self.assertFalse((self.root / "cache").exists())
        self.assertEqual(self.repo.db_path.stat().st_mtime_ns, modified_before)

    def test_downloads_deduplicates_and_aggregates_ocr_for_search(self) -> None:
        self.repo.upsert_messages(
            [
                make_message("1", f"{self.base_url}/a.png"),
                make_message("2", f"{self.base_url}/b.png"),
            ]
        )
        seen_paths: list[Path] = []

        def read_ocr(path: Path, lang: str, command: str) -> str:
            seen_paths.append(path)
            self.assertEqual(lang, "chi_sim+eng")
            self.assertEqual(command, "")
            return "图片里的活动通知"

        report = enrich_images(
            self.repo,
            self.root / "cache",
            "chi_sim+eng",
            apply=True,
            session=self.session,
            ocr_reader=read_ocr,
        )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["processed"], 2)
        self.assertEqual(report["downloaded"], 2)
        self.assertEqual(report["deduplicated_files"], 1)
        self.assertEqual(report["ocr_completed"], 2)
        self.assertEqual(len(seen_paths), 2)
        messages = self.repo.list_image_messages()
        attachments = self.repo.list_image_attachments(row["id"] for row in messages)
        self.assertEqual(len(attachments), 2)
        self.assertEqual({row["status"] for row in attachments}, {"completed"})
        self.assertEqual(len({row["local_path"] for row in attachments}), 1)
        self.assertEqual(len(list((self.root / "cache").rglob("*.png"))), 1)
        self.assertEqual(len(self.repo.search(content="图片里的活动通知")), 2)
        audit = self.repo.audit()
        self.assertEqual(audit["attachments"]["total"], 2)
        self.assertEqual(audit["attachments"]["with_ocr"], 2)
        self.assertEqual(audit["attachments"]["by_status"], {"completed": 2})

        self.repo.upsert_messages(
            [
                make_message("1", f"{self.base_url}/a.png?refreshed=1"),
                make_message("2", f"{self.base_url}/b.png"),
            ]
        )
        request_count = len(ImageHandler.requests)
        repeated = enrich_images(
            self.repo,
            self.root / "cache",
            "chi_sim+eng",
            apply=True,
            session=self.session,
            ocr_reader=read_ocr,
        )
        self.assertEqual(repeated["already_complete"], 2)
        self.assertEqual(repeated["processed"], 0)
        self.assertEqual(len(ImageHandler.requests), request_count)

    def test_failed_ocr_retries_from_cached_file_without_redownload(self) -> None:
        self.repo.upsert_messages([make_message("1", f"{self.base_url}/a.png")])

        def fail_ocr(_: Path, __: str, ___: str) -> str:
            raise RuntimeError("temporary OCR failure")

        failed = enrich_images(
            self.repo,
            self.root / "cache",
            "chi_sim+eng",
            apply=True,
            session=self.session,
            ocr_reader=fail_ocr,
        )
        self.assertEqual(failed["status"], "warning")
        self.assertEqual(failed["failed"], 1)
        self.assertEqual(len(ImageHandler.requests), 1)

        retried = enrich_images(
            self.repo,
            self.root / "cache",
            "chi_sim+eng",
            apply=True,
            session=self.session,
            ocr_reader=lambda *_: "重试识别成功",
        )
        self.assertEqual(retried["cache_hits"], 1)
        self.assertEqual(retried["ocr_completed"], 1)
        self.assertEqual(len(ImageHandler.requests), 1)
        self.assertEqual(len(self.repo.search(content="重试识别成功")), 1)

    def test_legacy_schema_preview_does_not_migrate(self) -> None:
        legacy_path = self.root / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as conn, conn:
            conn.execute(CREATE_TABLE_SQL)
            conn.execute(
                """
                INSERT INTO essence_messages (
                    sender, sender_time, essence_time, operator, content_text,
                    content_type, image_path, content_search, source
                ) VALUES (?, '', '', '管理员', '旧图片', 'image', ?, '旧图片', 'onebot')
                """,
                ("旧用户", f"{self.base_url}/a.png"),
            )
        modified_before = legacy_path.stat().st_mtime_ns

        report = enrich_images(
            EssenceRepository(legacy_path),
            self.root / "legacy-cache",
            "chi_sim+eng",
            apply=False,
            session=self.session,
        )

        self.assertEqual(report["pending"], 1)
        self.assertEqual(legacy_path.stat().st_mtime_ns, modified_before)
        with closing(sqlite3.connect(legacy_path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'essence_attachments'"
                ).fetchone()
            )


if __name__ == "__main__":
    unittest.main()

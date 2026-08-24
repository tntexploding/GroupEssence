from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from starlette.requests import Request

from group_essence_extractor.api import SearchQuery, SearchRequest, create_app
from group_essence_extractor.cli import build_parser, main
from group_essence_extractor.config import Settings
from group_essence_extractor.db import EssenceRepository
from group_essence_extractor.models import EssenceMessage


def make_settings(root: Path) -> Settings:
    return Settings(
        db_path=root / "api.db",
        onebot_base_url="http://onebot.test",
        onebot_access_token="",
        group_id="123456",
        prefer_onebot=False,
        fallback_ocr=False,
        ocr_lang="chi_sim+eng",
        tesseract_cmd="",
        screenshot_dir=root / "screenshots",
        image_dir=root / "images",
    )


def make_request(app: object, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("test", 50000),
            "server": ("test", 80),
            "root_path": "",
            "app": app,
        }
    )


class ApiAndCliTests(unittest.TestCase):
    def test_app_factory_defers_database_initialization_to_lifespan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = make_settings(root)
            repo = EssenceRepository(settings.db_path)
            app = create_app(settings, repo)
            self.assertFalse(settings.db_path.exists())

            async def run_lifespan() -> None:
                async with app.router.lifespan_context(app):
                    self.assertTrue(settings.db_path.exists())

            asyncio.run(run_lifespan())

    def test_health_and_search_routes_use_injected_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = make_settings(root)
            repo = EssenceRepository(settings.db_path)
            repo.init_db()
            repo.upsert_messages(
                [
                    EssenceMessage(
                        sender="测试用户",
                        sender_time="2026-05-01 10:00:00",
                        essence_time="2026-05-01 10:05:00",
                        operator="管理员",
                        content_text="活动通知",
                        group_id="123456",
                        message_id="1",
                        source="onebot",
                    )
                ]
            )
            app = create_app(settings, repo)
            routes = {route.path: route for route in app.routes if hasattr(route, "path")}

            self.assertEqual(routes["/health"].endpoint(), {"status": "ok"})
            result = routes["/api/v1/search"].endpoint(
                req=SearchRequest(request_id="req-1", query=SearchQuery(content="活动")),
                request=make_request(app, "/api/v1/search"),
            )
            self.assertEqual(result["request_id"], "req-1")
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["limit"], 100)
            self.assertEqual(result["items"][0]["content_text"], "活动通知")

    def test_search_query_validates_pagination(self) -> None:
        with self.assertRaises(ValidationError):
            SearchQuery(limit=0)
        with self.assertRaises(ValidationError):
            SearchQuery(offset=-1)

    def test_cli_parser_exposes_expected_commands(self) -> None:
        parser = build_parser()
        self.assertEqual(parser.parse_args(["init-db"]).command, "init-db")
        self.assertEqual(parser.parse_args(["doctor"]).command, "doctor")
        self.assertTrue(parser.parse_args(["doctor", "--images"]).images)
        self.assertEqual(parser.parse_args(["audit-db"]).command, "audit-db")
        self.assertFalse(parser.parse_args(["repair-db"]).apply)
        self.assertTrue(parser.parse_args(["repair-db", "--apply"]).apply)
        enrich = parser.parse_args(
            ["enrich-images", "--apply", "--group-id", "123456", "--limit", "5"]
        )
        self.assertTrue(enrich.apply)
        self.assertEqual(enrich.group_id, "123456")
        self.assertEqual(enrich.limit, 5)
        self.assertTrue(parser.parse_args(["ingest", "--dry-run"]).dry_run)
        search = parser.parse_args(
            [
                "search",
                "--content",
                "活动",
                "--group-id",
                "123456",
                "--sender-time-from",
                "2026-05-01",
                "--limit",
                "20",
            ]
        )
        self.assertEqual(search.command, "search")
        self.assertEqual(search.content, "活动")
        self.assertEqual(search.group_id, "123456")
        self.assertEqual(search.sender_time_from, "2026-05-01")
        self.assertEqual(search.limit, 20)
        export = parser.parse_args(
            ["export", "--format", "json", "--output", "records.json", "--max-records", "10"]
        )
        self.assertEqual(export.command, "export")
        self.assertEqual(export.max_records, 10)

    def test_invalid_image_apply_options_do_not_initialize_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = make_settings(Path(temp))
            with patch("group_essence_extractor.cli.get_settings", return_value=settings), redirect_stdout(
                StringIO()
            ):
                exit_code = main(["enrich-images", "--apply", "--limit", "0"])

            self.assertEqual(exit_code, 1)
            self.assertFalse(settings.db_path.exists())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from group_essence_extractor.db import EssenceRepository, SearchPage
from group_essence_extractor.models import EssenceMessage
from group_essence_extractor.plugin_service import (
    GroupEssencePluginService,
    PluginServiceError,
    format_search_page,
)


def make_message(
    *,
    group_id: str = "123456",
    message_id: str = "message-1",
    content: str = "活动通知",
) -> EssenceMessage:
    return EssenceMessage(
        sender="发送者",
        sender_id="10001",
        sender_time="2026-08-24 20:00:00",
        essence_time="2026-08-24 20:05:00",
        operator="管理员",
        operator_id="10002",
        content_text=content,
        content_type="text",
        group_id=group_id,
        message_id=message_id,
        source="onebot",
        raw_data={"message_id": message_id},
    )


class FakeSource:
    def __init__(self, messages: list[EssenceMessage], *, pause: bool = False) -> None:
        self.messages = messages
        self.pause = pause
        self.active = 0
        self.max_active = 0

    async def get_essence_messages(
        self,
        _: object,
        __: str,
    ) -> list[EssenceMessage]:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.pause:
            await asyncio.sleep(0.02)
        self.active -= 1
        return self.messages


class PluginServiceTests(unittest.TestCase):
    def test_sync_is_idempotent_and_queries_are_scoped_to_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = EssenceRepository(Path(temp) / "plugin" / "group_essence.db")
            source = FakeSource([make_message()])
            service = GroupEssencePluginService(
                source=source,  # type: ignore[arg-type]
                repository=repository,
            )

            first = asyncio.run(service.sync(object(), "123456"))
            second = asyncio.run(service.sync(object(), "123456"))
            repository.upsert_messages(
                [
                    make_message(
                        group_id="654321",
                        message_id="other-group",
                        content="活动通知（其他群）",
                    )
                ]
            )
            search = asyncio.run(service.search("123456", "活动", 20))
            recent = asyncio.run(service.recent("123456", 20))

            self.assertEqual((first.inserted, first.unchanged), (1, 0))
            self.assertEqual((second.inserted, second.unchanged), (0, 1))
            self.assertEqual(search.total, 1)
            self.assertEqual(recent.total, 1)
            self.assertTrue(
                all(item["group_id"] == "123456" for item in search.items + recent.items)
            )

    def test_search_rejects_empty_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = GroupEssencePluginService(
                source=FakeSource([]),  # type: ignore[arg-type]
                repository=EssenceRepository(Path(temp) / "missing.db"),
            )

            with self.assertRaises(PluginServiceError) as raised:
                asyncio.run(service.search("123456", "   ", 5))

            self.assertEqual(raised.exception.category, "empty_keyword")

    def test_sqlite_work_is_dispatched_to_threads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = EssenceRepository(Path(temp) / "group_essence.db")
            service = GroupEssencePluginService(
                source=FakeSource([make_message()]),  # type: ignore[arg-type]
                repository=repository,
            )
            original_to_thread = asyncio.to_thread
            calls: list[str] = []

            async def recording_to_thread(function: object, *args: object, **kwargs: object):
                calls.append(getattr(function, "__name__", type(function).__name__))
                return await original_to_thread(function, *args, **kwargs)  # type: ignore[arg-type]

            with patch(
                "group_essence_extractor.plugin_service.asyncio.to_thread",
                side_effect=recording_to_thread,
            ):
                asyncio.run(service.sync(object(), "123456"))
                asyncio.run(service.search("123456", "活动", 5))
                asyncio.run(service.status())

            self.assertIn("_persist_messages", calls)
            self.assertIn("search_page", calls)
            self.assertIn("audit", calls)

    def test_concurrent_syncs_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = FakeSource([make_message()], pause=True)
            service = GroupEssencePluginService(
                source=source,  # type: ignore[arg-type]
                repository=EssenceRepository(Path(temp) / "group_essence.db"),
            )

            async def run_both() -> None:
                await asyncio.gather(
                    service.sync(object(), "123456"),
                    service.sync(object(), "123456"),
                )

            asyncio.run(run_both())

            self.assertEqual(source.max_active, 1)

    def test_search_output_omits_internal_fields_and_redacts_secrets(self) -> None:
        page = SearchPage(
            items=[
                {
                    "sender": "发送者",
                    "sender_time": "2026-08-24 20:00:00",
                    "essence_time": "2026-08-24 20:05:00",
                    "content_text": (
                        "访问 https://private.invalid/path token=secret-token "
                        r"C:\private\file.txt /root/private/file.txt 后继续查看很长的正文"
                    ),
                    "content_type": "text",
                    "raw_json": "raw-json-value",
                    "remote_url": "https://remote.invalid/image.png",
                }
            ],
            total=2,
            limit=1,
            offset=0,
        )

        output = format_search_page(page, max_content_chars=80)

        self.assertNotIn("https://", output)
        self.assertNotIn("secret-token", output)
        self.assertNotIn(r"C:\private", output)
        self.assertNotIn("/root/private", output)
        self.assertNotIn("raw-json-value", output)
        self.assertNotIn("remote.invalid", output)
        self.assertIn("另有 1 条结果未显示", output)


if __name__ == "__main__":
    unittest.main()

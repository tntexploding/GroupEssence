from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from group_essence_extractor.db import EssenceRepository, SearchPage
from group_essence_extractor.astrbot_source import OneBotActionError
from group_essence_extractor.models import EssenceMessage, MessageTimeRecord
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
    sender_time: str = "2026-08-24 20:00:00",
) -> EssenceMessage:
    return EssenceMessage(
        sender="发送者",
        sender_id="10001",
        sender_time=sender_time,
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
    def __init__(
        self,
        messages: list[EssenceMessage],
        *,
        pause: bool = False,
        history: list[MessageTimeRecord] | None = None,
        history_error: bool = False,
    ) -> None:
        self.messages = messages
        self.pause = pause
        self.history = history or []
        self.history_error = history_error
        self.active = 0
        self.max_active = 0
        self.history_calls = 0
        self.skip_detail_ids: set[str] = set()

    async def get_essence_messages(
        self,
        _: object,
        __: str,
        *,
        detail_request_limit: int | None = None,
        skip_detail_ids: Iterable[str] = (),
    ) -> list[EssenceMessage]:
        self.skip_detail_ids = {str(value) for value in skip_detail_ids}
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.pause:
            await asyncio.sleep(0.02)
        self.active -= 1
        return self.messages

    async def get_group_history_times(
        self,
        _: object,
        __: str,
        *,
        limit: int,
    ) -> list[MessageTimeRecord]:
        self.history_calls += 1
        if self.history_error:
            raise OneBotActionError(
                "get_group_msg_history",
                status="failed",
                retcode=1404,
            )
        return self.history[:limit]


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

    def test_sync_enriches_only_new_missing_times_with_one_history_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            message = make_message(sender_time="")
            message.raw_data = {
                "essence": {
                    "message_id": "message-1",
                    "msg_seq": "200",
                    "msg_random": "300",
                }
            }
            source = FakeSource(
                [message],
                history=[
                    MessageTimeRecord(
                        sender_time="2026-08-24 20:00:00",
                        message_seq="200",
                        message_random="300",
                    )
                ],
            )
            repository = EssenceRepository(Path(temp) / "group_essence.db")
            service = GroupEssencePluginService(
                source=source,  # type: ignore[arg-type]
                repository=repository,
                history_query_limit=100,
            )

            first = asyncio.run(service.sync(object(), "123456"))
            second = asyncio.run(service.sync(object(), "123456"))

            self.assertEqual(first.sender_times_enriched, 1)
            self.assertEqual(source.history_calls, 1)
            self.assertEqual(second.sender_times_enriched, 0)
            self.assertEqual(second.unchanged, 1)
            self.assertEqual(
                repository.search(sender_qq="10001")[0]["sender_time"],
                "2026-08-24 20:00:00",
            )

    def test_history_lookup_failure_does_not_block_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            message = make_message(sender_time="")
            source = FakeSource([message], history_error=True)
            repository = EssenceRepository(Path(temp) / "group_essence.db")
            service = GroupEssencePluginService(
                source=source,  # type: ignore[arg-type]
                repository=repository,
                history_query_limit=100,
            )

            report = asyncio.run(service.sync(object(), "123456"))

            self.assertEqual(report.inserted, 1)
            self.assertTrue(report.history_lookup_failed)
            self.assertEqual(source.history_calls, 1)
            self.assertEqual(
                repository.search(sender_qq="10001")[0]["sender_time"],
                "",
            )

    def test_explicit_sender_time_repair_is_bounded_and_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = EssenceRepository(Path(temp) / "group_essence.db")
            repository.init_db()
            missing = make_message(sender_time="")
            repository.upsert_messages([missing])
            source = FakeSource(
                [],
                history=[
                    MessageTimeRecord(
                        sender_time="2026-08-24 20:00:00",
                        message_id="message-1",
                    )
                ],
            )
            service = GroupEssencePluginService(
                source=source,  # type: ignore[arg-type]
                repository=repository,
                history_query_limit=20,
            )

            report = asyncio.run(
                service.repair_sender_times(object(), "123456", "10")
            )

            self.assertEqual(report.history_scanned, 1)
            self.assertEqual((report.updated, report.remaining), (1, 0))
            self.assertEqual(source.history_calls, 1)

    def test_sync_skips_previously_failed_content_detail_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = EssenceRepository(Path(temp) / "group_essence.db")
            repository.init_db()
            missing = make_message(content="[空消息]")
            missing.raw_data = {
                "essence": {"message_id": "message-1"},
                "message_detail_error": "OneBot action=get_msg",
            }
            repository.upsert_messages([missing])
            source = FakeSource([missing])
            service = GroupEssencePluginService(
                source=source,  # type: ignore[arg-type]
                repository=repository,
            )

            asyncio.run(service.sync(object(), "123456"))

            self.assertEqual(source.skip_detail_ids, {"message-1"})

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

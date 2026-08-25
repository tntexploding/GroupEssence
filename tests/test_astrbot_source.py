from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from group_essence_extractor.astrbot_source import (
    AstrBotEssenceSource,
    OneBotActionError,
    apply_history_sender_times,
    unwrap_action_result,
)
from group_essence_extractor.models import EssenceMessage, MessageTimeRecord


class FakeActionApi:
    def __init__(self, responses: dict[str, list[object]]) -> None:
        self.responses = {name: list(values) for name, values in responses.items()}
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_action(self, *, action: str, **params: object) -> object:
        self.calls.append((action, params))
        response = self.responses[action].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_event(api: object) -> SimpleNamespace:
    return SimpleNamespace(bot=SimpleNamespace(api=api))


class AstrBotSourceTests(unittest.TestCase):
    def test_full_envelope_is_unwrapped_and_missing_fields_use_get_msg(self) -> None:
        api = FakeActionApi(
            {
                "get_essence_msg_list": [
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": [
                            {
                                "sender_id": 10001,
                                "operator_id": 10002,
                                "operator_time": 1_700_000_100,
                                "message_id": 42,
                            }
                        ],
                    }
                ],
                "get_msg": [
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {
                            "time": 1_700_000_000,
                            "group_id": 123456,
                            "message": [
                                {"type": "text", "data": {"text": "脱敏正文"}}
                            ],
                        },
                    }
                ],
            }
        )

        messages = asyncio.run(
            AstrBotEssenceSource().get_essence_messages(make_event(api), "123456")
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].group_id, "123456")
        self.assertEqual(messages[0].message_id, "42")
        self.assertEqual(messages[0].content_text, "脱敏正文")
        self.assertTrue(messages[0].sender_time)
        self.assertEqual(
            api.calls,
            [
                ("get_essence_msg_list", {"group_id": 123456}),
                ("get_msg", {"message_id": "42"}),
            ],
        )

    def test_already_unwrapped_result_does_not_request_unneeded_detail(self) -> None:
        api = FakeActionApi(
            {
                "get_essence_msg_list": [
                    [
                        {
                            "sender_time": 1_700_000_000,
                            "operator_time": 1_700_000_100,
                            "message_id": "m-1",
                            "content": [{"type": "text", "data": {"text": "正文"}}],
                        }
                    ]
                ]
            }
        )

        messages = asyncio.run(
            AstrBotEssenceSource().get_essence_messages(make_event(api), "group-test")
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(api.calls, [("get_essence_msg_list", {"group_id": "group-test"})])

    def test_content_present_without_sender_time_does_not_request_detail(self) -> None:
        api = FakeActionApi(
            {
                "get_essence_msg_list": [
                    [
                        {
                            "operator_time": 1_700_000_100,
                            "message_id": "historical-1",
                            "content": [
                                {"type": "text", "data": {"text": "已有正文"}}
                            ],
                        }
                    ]
                ]
            }
        )

        message = asyncio.run(
            AstrBotEssenceSource().get_essence_messages(make_event(api), "123456")
        )[0]

        self.assertEqual(message.sender_time, "")
        self.assertTrue(message.essence_time)
        self.assertEqual(message.content_text, "已有正文")
        self.assertNotIn("message_detail_requested", message.raw_data or {})
        self.assertEqual(api.calls, [("get_essence_msg_list", {"group_id": 123456})])

    def test_validation_detail_requests_honor_limit(self) -> None:
        api = FakeActionApi(
            {
                "get_essence_msg_list": [
                    [
                        {"message_id": f"missing-{index}", "operator_time": 1}
                        for index in range(3)
                    ]
                ],
                "get_msg": [
                    {"message": [{"type": "text", "data": {"text": "补全一"}}]},
                    {"message": [{"type": "text", "data": {"text": "补全二"}}]},
                ],
            }
        )

        messages = asyncio.run(
            AstrBotEssenceSource().get_essence_messages(
                make_event(api),
                "123456",
                detail_request_limit=2,
            )
        )

        self.assertEqual([call[0] for call in api.calls].count("get_msg"), 2)
        self.assertEqual(
            sum(
                bool((message.raw_data or {}).get("message_detail_requested"))
                for message in messages
            ),
            2,
        )
        self.assertEqual(messages[2].content_text, "[空消息]")

    def test_failed_detail_is_safely_recorded_without_dropping_item(self) -> None:
        api = FakeActionApi(
            {
                "get_essence_msg_list": [[{"message_id": "m-1"}]],
                "get_msg": [
                    {
                        "status": "failed",
                        "retcode": 1404,
                        "wording": (
                            "token=super-secret https://private.invalid/path "
                            "for group 123456789"
                        ),
                        "data": None,
                    }
                ],
            }
        )

        message = asyncio.run(
            AstrBotEssenceSource().get_essence_messages(make_event(api), "123456")
        )[0]
        error = str((message.raw_data or {}).get("message_detail_error") or "")

        self.assertIn("action=get_msg", error)
        self.assertIn("retcode=1404", error)
        self.assertNotIn("super-secret", error)
        self.assertNotIn("private.invalid", error)
        self.assertNotIn("123456789", error)

    def test_previous_detail_failure_can_be_skipped(self) -> None:
        api = FakeActionApi(
            {"get_essence_msg_list": [[{"message_id": "m-1", "operator_time": 1}]]}
        )

        message = asyncio.run(
            AstrBotEssenceSource().get_essence_messages(
                make_event(api),
                "123456",
                detail_request_limit=10,
                skip_detail_ids={"m-1"},
            )
        )[0]

        self.assertEqual(message.content_text, "[空消息]")
        self.assertEqual(api.calls, [("get_essence_msg_list", {"group_id": 123456})])

    def test_group_history_extracts_only_time_identity_and_enriches_by_sequence(self) -> None:
        api = FakeActionApi(
            {
                "get_group_msg_history": [
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {
                            "messages": [
                                {
                                    "message_id": "history-id",
                                    "message_seq": "200",
                                    "msg_random": "300",
                                    "time": 1_700_000_000,
                                    "raw_message": "不得进入时间索引的正文",
                                }
                            ]
                        },
                    }
                ]
            }
        )
        records = asyncio.run(
            AstrBotEssenceSource().get_group_history_times(
                make_event(api),
                "123456",
                limit=100,
            )
        )
        message = EssenceMessage(
            sender="发送者",
            sender_time="",
            essence_time="2026-05-01 10:05:00",
            operator="管理员",
            content_text="正文",
            group_id="123456",
            message_id="synthetic-id",
            source="onebot",
            raw_data={"essence": {"msg_seq": "200", "msg_random": "300"}},
        )

        enriched, changed = apply_history_sender_times(
            [message],
            records,
            candidate_message_ids={"synthetic-id"},
        )

        self.assertEqual(changed, 1)
        self.assertTrue(enriched[0].sender_time)
        self.assertEqual((enriched[0].raw_data or {})["sender_time_source"], "group_history")
        self.assertNotIn("raw_message", vars(records[0]))
        self.assertEqual(
            api.calls,
            [("get_group_msg_history", {"group_id": 123456, "count": 100})],
        )

    def test_group_history_is_locally_bounded_when_adapter_returns_too_much(self) -> None:
        api = FakeActionApi(
            {
                "get_group_msg_history": [
                    {
                        "messages": [
                            {"message_id": f"history-{index}", "time": 1_700_000_000}
                            for index in range(3)
                        ]
                    }
                ]
            }
        )

        records = asyncio.run(
            AstrBotEssenceSource().get_group_history_times(
                make_event(api),
                "123456",
                limit=2,
            )
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(
            api.calls,
            [("get_group_msg_history", {"group_id": 123456, "count": 2})],
        )

    def test_ambiguous_history_sequence_does_not_fill_wrong_time(self) -> None:
        message = EssenceMessage(
            sender="发送者",
            sender_time="",
            essence_time="2026-05-01 10:05:00",
            operator="管理员",
            content_text="正文",
            group_id="123456",
            message_id="synthetic-id",
            source="onebot",
            raw_data={"essence": {"msg_seq": "200"}},
        )
        history = [
            MessageTimeRecord(
                sender_time="",
                message_id="synthetic-id",
            ),
            MessageTimeRecord(
                sender_time="2026-05-01 10:00:00",
                message_id="history-1",
                message_seq="200",
            ),
            MessageTimeRecord(
                sender_time="2026-05-01 10:01:00",
                message_id="history-2",
                message_seq="200",
            ),
        ]

        enriched, changed = apply_history_sender_times([message], history)

        self.assertEqual(changed, 0)
        self.assertEqual(enriched[0].sender_time, "")

    def test_invalid_list_and_failed_envelope_raise_public_error(self) -> None:
        invalid_api = FakeActionApi({"get_essence_msg_list": [{"unexpected": "dict"}]})
        with self.assertRaises(OneBotActionError) as invalid_context:
            asyncio.run(
                AstrBotEssenceSource().get_essence_messages(
                    make_event(invalid_api),
                    "123",
                )
            )
        self.assertIn("invalid_data", invalid_context.exception.public_message)

        failed = {
            "status": "failed",
            "retcode": 100,
            "wording": "token=do-not-leak https://private.invalid",
            "data": {"private": "payload"},
        }
        with self.assertRaises(OneBotActionError) as failed_context:
            unwrap_action_result(failed, action="get_essence_msg_list")
        public = failed_context.exception.public_message
        self.assertNotIn("do-not-leak", public)
        self.assertNotIn("private.invalid", public)
        self.assertNotIn("payload", public)

    def test_missing_action_api_is_reported_without_event_details(self) -> None:
        with self.assertRaises(OneBotActionError) as context:
            asyncio.run(
                AstrBotEssenceSource().get_essence_messages(
                    SimpleNamespace(bot=SimpleNamespace()),
                    "123",
                )
            )
        self.assertIn("action=resolve_api", context.exception.public_message)


if __name__ == "__main__":
    unittest.main()

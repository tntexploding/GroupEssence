from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from group_essence_extractor.astrbot_source import (
    AstrBotEssenceSource,
    OneBotActionError,
    unwrap_action_result,
)


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

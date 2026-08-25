from __future__ import annotations

from datetime import datetime
import unittest

from group_essence_extractor.fetchers import OneBotClient
from group_essence_extractor.normalization import (
    format_timestamp,
    normalize_essence_items,
    parse_message_content,
)


class StubResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class StubSession:
    def __init__(self, payloads: list[dict]) -> None:
        self.responses = [StubResponse(payload) for payload in payloads]
        self.calls: list[dict] = []
        self.closed = False

    def post(self, url: str, **kwargs: object) -> StubResponse:
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected OneBot request")
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def make_client(payloads: list[dict]) -> tuple[OneBotClient, StubSession]:
    client = OneBotClient("http://onebot.test", "token")
    client.session.close()
    session = StubSession(payloads)
    client.session = session  # type: ignore[assignment]
    return client, session


class OneBotClientTests(unittest.TestCase):
    def test_enriches_missing_time_and_content_from_message_detail(self) -> None:
        sender_ts = 1_700_000_000
        operator_ts = 1_700_000_100
        client, session = make_client(
            [
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": [
                        {
                            "sender_id": 10001,
                            "sender_nick": "发送者",
                            "operator_id": 10002,
                            "operator_nick": "管理员",
                            "operator_time": operator_ts,
                            "message_id": 42,
                        }
                    ],
                },
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {
                        "time": sender_ts,
                        "group_id": "123456",
                        "sender": {"user_id": "10001", "nickname": "详情昵称"},
                        "message": [
                            {"type": "text", "data": {"text": "活动通知"}},
                            {
                                "type": "image",
                                "data": {"url": "https://example.test/image.png"},
                            },
                        ],
                    },
                },
            ]
        )

        messages = client.get_essence_messages("123456")
        client.close()

        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(
            message.sender_time,
            datetime.fromtimestamp(sender_ts).strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.assertEqual(message.group_id, "123456")
        self.assertEqual(message.message_id, "42")
        self.assertEqual(message.content_text, "活动通知")
        self.assertEqual(message.content_type, "mixed")
        self.assertEqual(message.image_path, "https://example.test/image.png")
        self.assertIn("message_detail", message.raw_data or {})
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["json"], {"group_id": "123456"})
        self.assertTrue(session.calls[1]["url"].endswith("/get_msg"))
        self.assertTrue(session.closed)

    def test_keeps_list_content_and_uses_requested_group_id(self) -> None:
        client, session = make_client(
            [
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": [
                        {
                            "sender_id": "1",
                            "sender_nick": "A",
                            "sender_time": 1_700_000_000,
                            "operator_id": "2",
                            "operator_nick": "B",
                            "operator_time": 1_700_000_100,
                            "message_id": "m-1",
                            "content": [
                                {"type": "image", "data": {"file": "a.png"}},
                                {"type": "image", "data": {"url": "b.png"}},
                            ],
                        }
                    ],
                }
            ]
        )

        message = client.get_essence_messages("987654")[0]
        client.close()

        self.assertEqual(message.group_id, "987654")
        self.assertEqual(message.content_type, "image")
        self.assertEqual(message.image_path, "a.png\nb.png")
        self.assertEqual(len(session.calls), 1)

    def test_rejects_missing_group_id(self) -> None:
        client, _ = make_client([])
        with self.assertRaisesRegex(ValueError, "GROUP_ID"):
            client.get_essence_messages("")
        client.close()

    def test_reports_onebot_error_response(self) -> None:
        client, _ = make_client(
            [{"status": "failed", "retcode": 1404, "wording": "not found", "data": None}]
        )
        with self.assertRaisesRegex(RuntimeError, "retcode=1404"):
            client.get_essence_messages("123")
        client.close()

    def test_timestamp_units_are_normalized(self) -> None:
        seconds = 1_700_000_000
        expected = datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(format_timestamp(seconds), expected)
        self.assertEqual(format_timestamp(seconds * 1000), expected)
        self.assertEqual(format_timestamp(seconds * 1_000_000), expected)
        self.assertEqual(format_timestamp("not-a-timestamp"), "not-a-timestamp")

    def test_message_segments_remain_searchable(self) -> None:
        text, content_type, image_path, _ = parse_message_content(
            {
                "content": [
                    {"type": "at", "data": {"qq": "10001"}},
                    {"type": "reply", "data": {"id": "2"}},
                    {"type": "face", "data": {"id": "14"}},
                    {"type": "record", "data": {"file": "voice.amr"}},
                ]
            }
        )
        self.assertEqual(content_type, "text")
        self.assertEqual(image_path, "")
        self.assertEqual(text, "@10001[回复消息][表情:14][record]")

    def test_transport_independent_batch_normalization_keeps_detail_errors(self) -> None:
        messages = normalize_essence_items(
            [
                {
                    "sender_id": 10001,
                    "operator_id": 10002,
                    "operator_time": 1_700_000_100,
                    "message_id": 42,
                }
            ],
            requested_group_id="123456",
            detail_errors={"42": "get_msg failed"},
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].group_id, "123456")
        self.assertEqual(messages[0].message_id, "42")
        self.assertEqual(
            (messages[0].raw_data or {}).get("message_detail_error"),
            "get_msg failed",
        )


if __name__ == "__main__":
    unittest.main()

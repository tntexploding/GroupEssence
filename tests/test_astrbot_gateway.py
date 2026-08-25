from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from group_essence_extractor.astrbot_gateway import AstrBotOneBotGateway
from group_essence_extractor.astrbot_source import (
    AstrBotEssenceSource,
    OneBotActionError,
)


class FakeClient:
    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_action(self, action: str, **params: object) -> object:
        self.calls.append((action, params))
        response = self.responses.get(action, {})
        if isinstance(response, Exception):
            raise response
        return response


class FakePlatform:
    def __init__(self, client: FakeClient, name: str = "aiocqhttp") -> None:
        self.client = client
        self.name = name

    def meta(self) -> SimpleNamespace:
        return SimpleNamespace(name=self.name)

    def get_client(self) -> FakeClient:
        return self.client


class FakeContext:
    def __init__(self, platform: object | None) -> None:
        self.platform = platform
        self.requested_ids: list[str] = []

    def get_platform_inst(self, platform_id: str) -> object | None:
        self.requested_ids.append(platform_id)
        return self.platform


class AstrBotOneBotGatewayTests(unittest.TestCase):
    def test_gateway_resolves_configured_platform_for_background_collection(self) -> None:
        client = FakeClient(
            {
                "get_essence_msg_list": {
                    "status": "ok",
                    "retcode": 0,
                    "data": [
                        {
                            "message_id": "m-1",
                            "operator_time": 1_700_000_100,
                            "content": [
                                {"type": "text", "data": {"text": "后台采集"}}
                            ],
                        }
                    ],
                }
            }
        )
        context = FakeContext(FakePlatform(client))
        gateway = AstrBotOneBotGateway(context, "platform-1")

        messages = asyncio.run(
            AstrBotEssenceSource().get_essence_messages(gateway, "123456")
        )

        self.assertEqual(context.requested_ids, ["platform-1"])
        self.assertEqual(messages[0].content_text, "后台采集")
        self.assertEqual(
            client.calls,
            [("get_essence_msg_list", {"group_id": 123456})],
        )

    def test_private_alert_uses_onebot_action_and_numeric_user_id(self) -> None:
        client = FakeClient(
            {
                "send_private_msg": {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": 1},
                }
            }
        )
        gateway = AstrBotOneBotGateway(
            FakeContext(FakePlatform(client)),
            "platform-1",
        )

        asyncio.run(gateway.send_private_text("10001", "同步异常"))

        self.assertEqual(
            client.calls,
            [
                (
                    "send_private_msg",
                    {"user_id": 10001, "message": "同步异常"},
                )
            ],
        )

    def test_resolution_and_client_errors_are_redacted(self) -> None:
        missing = AstrBotOneBotGateway(FakeContext(None), "platform-secret")
        with self.assertRaises(OneBotActionError) as missing_error:
            asyncio.run(missing.call_action(action="get_msg", message_id="secret"))
        self.assertIn("status=not_found", str(missing_error.exception))
        self.assertNotIn("platform-secret", str(missing_error.exception))

        wrong = AstrBotOneBotGateway(
            FakeContext(FakePlatform(FakeClient(), name="telegram")),
            "platform-1",
        )
        with self.assertRaises(OneBotActionError) as wrong_error:
            asyncio.run(wrong.call_action(action="get_msg"))
        self.assertIn("status=wrong_adapter", str(wrong_error.exception))

        class BrokenPlatform:
            def meta(self) -> object:
                raise RuntimeError("token=platform-secret")

        broken = AstrBotOneBotGateway(FakeContext(BrokenPlatform()), "platform-1")
        with self.assertRaises(OneBotActionError) as broken_error:
            asyncio.run(broken.call_action(action="get_msg"))
        self.assertIn("status=exception", str(broken_error.exception))
        self.assertNotIn("platform-secret", str(broken_error.exception))

        failing_client = FakeClient(
            {"get_msg": RuntimeError("token=super-secret https://private.invalid")}
        )
        failing = AstrBotOneBotGateway(
            FakeContext(FakePlatform(failing_client)),
            "platform-1",
        )
        with self.assertRaises(OneBotActionError) as client_error:
            asyncio.run(failing.call_action(action="get_msg", message_id="m-1"))
        public = str(client_error.exception)
        self.assertIn("status=exception", public)
        self.assertNotIn("super-secret", public)
        self.assertNotIn("private.invalid", public)


if __name__ == "__main__":
    unittest.main()

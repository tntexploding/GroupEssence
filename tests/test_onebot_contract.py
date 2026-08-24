from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest

from group_essence_extractor.fetchers import OneBotClient


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "onebot"


class ContractHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(
            {
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization", ""),
            }
        )

        fixture_name = {
            "/get_essence_msg_list": "essence_list.json",
            "/get_msg": "message_detail.json",
        }.get(self.path)
        if fixture_name is None:
            self.send_error(404)
            return

        payload = (FIXTURE_DIR / fixture_name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _: str, *args: object) -> None:
        return None


class OneBotHttpContractTests(unittest.TestCase):
    def test_real_http_round_trip_matches_supported_contract(self) -> None:
        ContractHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), ContractHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            client = OneBotClient(
                f"http://{host}:{port}",
                access_token="contract-token",
                timeout_seconds=2,
            )
            client.session.trust_env = False
            messages = client.get_essence_messages("123456")
            client.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].group_id, "123456")
        self.assertTrue(messages[0].sender_time)
        self.assertEqual(messages[0].content_type, "mixed")
        self.assertEqual(messages[0].content_text, "脱敏契约测试消息")
        self.assertEqual(
            [request["path"] for request in ContractHandler.requests],
            ["/get_essence_msg_list", "/get_msg"],
        )
        self.assertEqual(ContractHandler.requests[0]["body"], {"group_id": "123456"})
        self.assertTrue(
            all(
                request["authorization"] == "Bearer contract-token"
                for request in ContractHandler.requests
            )
        )


if __name__ == "__main__":
    unittest.main()

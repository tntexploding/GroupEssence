from __future__ import annotations

import http.client
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from group_essence_extractor import image_reply as images


PNG = b"\x89PNG\r\n\x1a\nfixture-image"
URL = "https://group-digest-pi-1251316161.file.myqcloud.com/example.png?signature=private"
DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]


def row(group="123456", urls=URL):
    return {"group_id": group, "content_type": "image", "image_path": urls}


class CacheTests(unittest.TestCase):
    def test_on_demand_cache_reuses_bytes_without_database_or_url_file_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "reply_images"
            fetch = Mock(return_value=PNG)
            cache = images.ImageReplyCache(root, fetch)
            self.assertFalse(root.exists())
            self.assertEqual(cache.prepare([], 5), ([], 0))
            first, omitted = cache.prepare([row()], 5)
            second, _ = cache.prepare([row()], 5)
            self.assertEqual(first[0].data, PNG)
            self.assertEqual(second[0].data, PNG)
            self.assertEqual(omitted, 0)
            self.assertEqual(fetch.call_count, 1)
            files = list(root.rglob("*.img"))
            self.assertEqual(len(files), 1)
            self.assertNotIn("signature", files[0].name)
            self.assertEqual(files[0].parent.name, "123456")

    def test_group_caches_do_not_share_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            fetch = Mock(return_value=PNG)
            cache = images.ImageReplyCache(Path(temp), fetch)
            cache.prepare([row("123456"), row("654321")], 5)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(len(list(Path(temp).glob("*/*.img"))), 2)

    def test_caption_positions_and_limit_include_failed_images(self):
        with tempfile.TemporaryDirectory() as temp:
            fetch = Mock(side_effect=[PNG, OSError("secret-url")])
            cache = images.ImageReplyCache(Path(temp), fetch)
            rows = [{"content_type": "text"}, row(urls=URL + "\n" + URL + "-2"), row(urls=URL + "-3")]
            result, omitted = cache.prepare(rows, 2)
            self.assertEqual([(r.record_number, r.image_number, r.image_total) for r in result], [(2, 1, 2), (2, 2, 2)])
            self.assertEqual(result[0].data, PNG)
            self.assertIn("读取失败", result[1].error)
            self.assertNotIn("secret", result[1].error)
            self.assertEqual(omitted, 1)

    def test_disabled_does_not_download_or_create_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "images"
            fetch = Mock()
            self.assertEqual(images.ImageReplyCache(root, fetch).prepare([row()], 0), ([], 1))
            fetch.assert_not_called()
            self.assertFalse(root.exists())

    def test_invalid_group_and_untrusted_bytes_do_not_escape_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            fetch = Mock(return_value=b"<html>not an image</html>")
            cache = images.ImageReplyCache(Path(temp), fetch)
            for group in ("../escape", "１２３４５６", ""):
                result, _ = cache.prepare([row(group)], 1)
                self.assertTrue(result[0].error)
            fetch.assert_not_called()
            result, _ = cache.prepare([row()], 1)
            self.assertTrue(result[0].error)
            self.assertEqual(list(Path(temp).rglob("*.img")), [])

    def test_total_download_deadline_stops_later_downloads(self):
        with tempfile.TemporaryDirectory() as temp:
            clock = [0]
            def fetch(url, deadline):
                self.assertEqual(deadline, images.QUERY_DOWNLOAD_SECONDS)
                clock[0] = deadline + 1
                return PNG
            with patch.object(images.time, "monotonic", side_effect=lambda: clock[0]):
                result, _ = images.ImageReplyCache(Path(temp), fetch).prepare([row(urls=URL + "\n" + URL + "-2")], 5)
            self.assertEqual(result[0].data, PNG)
            self.assertIn("超时", result[1].error)

    def test_cache_eviction_only_removes_image_cache_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            unrelated = root / "database.db"
            unrelated.write_text("preserve")
            with patch.object(images, "MAX_CACHE_FILES", 1):
                cache = images.ImageReplyCache(root, Mock(return_value=PNG))
                result, _ = cache.prepare([row(), row("654321", URL + "-2")], 5)
            self.assertEqual(len(result), 2)
            self.assertEqual(len(list(root.glob("*/*.img"))), 1)
            self.assertEqual(unrelated.read_text(), "preserve")

    def test_cache_write_failure_still_returns_downloaded_image(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "not-a-directory"
            root.write_text("fixture")
            result, _ = images.ImageReplyCache(root, Mock(return_value=PNG)).prepare([row()], 5)
            self.assertEqual(result[0].data, PNG)


class DownloadTests(unittest.TestCase):
    def test_only_explicit_https_cdn_hosts_are_accepted(self):
        with patch.object(socket, "getaddrinfo", return_value=DNS) as dns:
            host, address, target = images._public_target(URL)
            self.assertEqual(address, "1.1.1.1")
            self.assertTrue(target.startswith("/example.png?"))
            for url in ("file:///etc/passwd", "http://gchat.qpic.cn/x", "https://example.com/x",
                        "https://gchat.qpic.cn.evil.example/x", "https://user:pass@gchat.qpic.cn/x",
                        "https://gchat.qpic.cn:8443/x", "https://127.0.0.1/x"):
                with self.subTest(url=url), self.assertRaises(images.ImageReplyError):
                    images._public_target(url)
            self.assertEqual(dns.call_count, 1)

    def test_private_or_mixed_dns_answers_are_rejected(self):
        for addr in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"):
            answers = DNS + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 443))]
            with patch.object(socket, "getaddrinfo", return_value=answers):
                with self.assertRaises(images.ImageReplyError):
                    images._public_target(URL)

    def test_connection_pins_validated_ip_and_retains_tls_hostname(self):
        conn = images._PinnedHTTPSConnection("gchat.qpic.cn", "1.1.1.1", 5)
        self.assertTrue(conn._context.check_hostname)
        context = Mock()
        conn._context = context
        with patch.object(socket, "create_connection") as connect:
            conn.connect()
        connect.assert_called_once_with(("1.1.1.1", 443), 5)
        context.wrap_socket.assert_called_once_with(connect.return_value, server_hostname="gchat.qpic.cn")

    def response(self, status=200, headers=None, chunks=None):
        result = Mock(status=status)
        result.getheader.side_effect = (headers or {}).get
        result.read1.side_effect = chunks or [PNG, b""]
        return result

    def download(self, responses):
        connection = Mock()
        connection.getresponse.side_effect = responses
        with patch.object(socket, "getaddrinfo", return_value=DNS), patch.object(images, "_PinnedHTTPSConnection", return_value=connection):
            return images._download_image(URL, time.monotonic() + 20), connection

    def test_download_success_and_close(self):
        data, connection = self.download([self.response()])
        self.assertEqual(data, PNG)
        connection.close.assert_called_once()

    def test_redirect_target_is_checked_before_connection(self):
        response = self.response(302, {"Location": "http://169.254.169.254/latest/meta-data"})
        with self.assertRaises(images.ImageReplyError):
            self.download([response])

    def test_oversized_headers_streams_and_non_images_are_rejected(self):
        cases = [self.response(headers={"Content-Length": str(images.MAX_IMAGE_BYTES + 1)}),
                 self.response(chunks=[b"x" * (images.MAX_IMAGE_BYTES + 1)]),
                 self.response(chunks=[b"<html>expired</html>", b""]),
                 self.response(status=403)]
        for response in cases:
            with self.subTest(response=response), self.assertRaises(images.ImageReplyError):
                self.download([response])


if __name__ == "__main__":
    unittest.main()

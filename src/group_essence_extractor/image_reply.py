"""Bounded, on-demand QQ image replies; no OCR, LLM or database writes."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import http.client
import ipaddress
import os
from pathlib import Path
import socket
import tempfile
import time
from typing import Any
from urllib.parse import urljoin, urlsplit


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_CACHE_BYTES = 128 * 1024 * 1024
MAX_CACHE_FILES = 256
QUERY_DOWNLOAD_SECONDS = 20
ALLOWED_HOSTS = frozenset({
    "group-digest-pi-1251316161.file.myqcloud.com",
    "multimedia.nt.qq.com", "multimedia.nt.qq.com.cn",
    "gchat.qpic.cn", "c2cpicdw.qpic.cn",
})


class ImageReplyError(ValueError):
    """Only fixed, public-safe messages; never include a source URL."""


@dataclass(frozen=True)
class ReplyImage:
    record_number: int
    image_number: int
    image_total: int
    data: bytes = b""
    error: str = ""


def _is_image(data: bytes) -> bool:
    return (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or data.startswith((b"GIF87a", b"GIF89a", b"BM"))
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )


def _public_target(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or host not in ALLOWED_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise ImageReplyError("图片来源不受支持。")
    addresses = list(dict.fromkeys(
        row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ))
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ImageReplyError("图片地址未通过安全检查。")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return host, addresses[0], target


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the validated IP, keeping the original TLS hostname verification."""

    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, timeout=timeout)
        self.address = address

    def connect(self) -> None:
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _download_image(url: str, deadline: float) -> bytes:
    for redirect in range(3):
        if time.monotonic() >= deadline:
            raise ImageReplyError("本次图片下载已超时。")
        host, address, target = _public_target(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ImageReplyError("本次图片下载已超时。")
        conn = _PinnedHTTPSConnection(host, address, min(5, remaining))
        try:
            conn.request("GET", target, headers={"Accept": "image/*", "User-Agent": "GroupEssence/0.5.1"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or redirect == 2:
                    raise ImageReplyError("图片重定向失败。")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ImageReplyError("图片暂不可用或地址已过期，可同步后重试。")
            length = response.getheader("Content-Length")
            if length and int(length) > MAX_IMAGE_BYTES:
                raise ImageReplyError("图片超过 5 MiB 限制。")
            payload = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ImageReplyError("本次图片下载已超时。")
                if conn.sock is not None:
                    conn.sock.settimeout(min(5, remaining))
                chunk = response.read1(min(65536, MAX_IMAGE_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > MAX_IMAGE_BYTES:
                    raise ImageReplyError("图片超过 5 MiB 限制。")
            data = bytes(payload)
            if not _is_image(data):
                raise ImageReplyError("图片响应不是支持的图片格式。")
            return data
        finally:
            conn.close()
    raise ImageReplyError("图片读取失败。")


class ImageReplyCache:
    def __init__(self, root: Path, fetcher: Callable[[str, float], bytes] | None = None):
        self.root = root
        self.fetcher = fetcher or _download_image

    def prepare(self, items: Sequence[Mapping[str, Any]], max_images: int) -> tuple[list[ReplyImage], int]:
        references = []
        for record, item in enumerate(items, 1):
            urls = list(dict.fromkeys(str(item.get("image_path") or "").splitlines()))
            urls = [url.strip() for url in urls if url.strip()]
            for position, url in enumerate(urls, 1):
                references.append((record, position, len(urls), str(item.get("group_id") or ""), url))
        limit = max(0, min(int(max_images), 10))
        deadline = time.monotonic() + QUERY_DOWNLOAD_SECONDS
        result = []
        for record, position, total, group, url in references[:limit]:
            try:
                data = self._read_or_fetch(group, url, deadline)
                result.append(ReplyImage(record, position, total, data=data))
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ImageReplyError) else "图片读取失败，请稍后重试。"
                result.append(ReplyImage(record, position, total, error=reason))
        return result, max(0, len(references) - limit)

    def _read_or_fetch(self, group: str, url: str, deadline: float) -> bytes:
        if not group.isascii() or not group.isdigit() or len(group) > 20:
            raise ImageReplyError("图片所属群无效。")
        folder = self.root / group
        # URL hashing is a cache key; it avoids storing signed URLs in file names.
        path = folder / (hashlib.sha256(url.encode()).hexdigest() + ".img")
        if self.root.is_symlink() or folder.is_symlink() or path.is_symlink():
            raise ImageReplyError("图片缓存路径无效。")
        if path.is_file() and path.stat().st_size <= MAX_IMAGE_BYTES:
            data = path.read_bytes()
            if _is_image(data):
                os.utime(path, None)
                return data
        if time.monotonic() >= deadline:
            raise ImageReplyError("本次图片下载已超时。")
        data = self.fetcher(url, deadline)
        if len(data) > MAX_IMAGE_BYTES or not _is_image(data):
            raise ImageReplyError("图片格式或大小不符合限制。")
        # Cache failure must not prevent returning an otherwise valid image.
        try:
            folder.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".download-", dir=folder)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                os.replace(name, path)
            finally:
                Path(name).unlink(missing_ok=True)
            self._prune()
        except OSError:
            pass
        return data

    def _prune(self) -> None:
        files = [p for p in self.root.glob("*/*.img") if not p.parent.is_symlink() and not p.is_symlink() and p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        count = len(files)
        for path in files:
            if total <= MAX_CACHE_BYTES and count <= MAX_CACHE_FILES:
                break
            total -= path.stat().st_size
            count -= 1
            path.unlink()

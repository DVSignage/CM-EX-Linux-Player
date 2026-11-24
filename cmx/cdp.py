"""
A screenshot of the renderer page straight from Chromium (DevTools protocol).

Grabbing the X screen returns black under rootless XWayland (stock Ubuntu on
Wayland), and some compositors refuse it altogether; Chromium can always draw
its own page. The kiosk listens on a loopback-only DevTools port; this module
asks the page for `Page.captureScreenshot` over a minimal websocket client
(standard library only — one request, one answer).
"""

import base64
import json
import os
import socket
import struct
import urllib.request
from typing import Optional
from urllib.parse import urlsplit

PORT = 9333


def _page_ws_url(port: int, prefix: str, timeout: float) -> Optional[str]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=timeout) as r:
        targets = json.load(r)
    for t in targets:
        if t.get("type") == "page" and str(t.get("url", "")).startswith(prefix) and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    return None


def _send(sock: socket.socket, text: str) -> None:
    payload = text.encode()
    head = bytearray([0x81])                    # FIN + text frame
    n = len(payload)
    if n < 126:
        head.append(0x80 | n)                   # masked (clients must mask)
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    mask = os.urandom(4)
    sock.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("DevTools socket closed")
        buf += chunk
    return bytes(buf)


def _recv_message(sock: socket.socket) -> str:
    """One whole message (fragments joined); control frames skipped."""
    parts = []
    while True:
        b0, b1 = _recv_exact(sock, 2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        mask = _recv_exact(sock, 4) if b1 & 0x80 else None
        data = _recv_exact(sock, n)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        opcode = b0 & 0x0F
        if opcode in (0x8, 0x9, 0xA):          # close / ping / pong
            if opcode == 0x8:
                raise ConnectionError("DevTools closed the connection")
            continue
        parts.append(data)
        if b0 & 0x80:
            return b"".join(parts).decode("utf-8", "replace")


def capture(page_prefix: str, port: int = PORT, quality: int = 70, timeout: float = 5.0) -> Optional[bytes]:
    """JPEG bytes of the page whose URL starts with `page_prefix`, or None."""
    try:
        ws = _page_ws_url(port, page_prefix, timeout)
        if not ws:
            return None
        u = urlsplit(ws)
        sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(1024)
                if not chunk:
                    return None
                head += chunk
            if b" 101 " not in head.split(b"\r\n", 1)[0]:
                return None
            _send(sock, json.dumps({"id": 1, "method": "Page.captureScreenshot",
                                    "params": {"format": "jpeg", "quality": quality}}))
            for _ in range(50):
                msg = json.loads(_recv_message(sock))
                if msg.get("id") == 1:
                    data = (msg.get("result") or {}).get("data")
                    return base64.b64decode(data) if data else None
        finally:
            sock.close()
    except (OSError, ValueError, ConnectionError, json.JSONDecodeError):
        return None
    return None

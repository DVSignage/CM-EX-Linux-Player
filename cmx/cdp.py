"""
The renderer page's Chromium, over its DevTools protocol.

- A screenshot straight from Chromium: grabbing the X screen returns black under
  rootless XWayland (stock Ubuntu on Wayland), and some compositors refuse it
  altogether; Chromium can always draw its own page.
- The kiosk window back over the whole screen when a screen change left it
  smaller (see fit_fullscreen).

The kiosk listens on a loopback-only DevTools port; this module talks to the
page over a minimal websocket client (standard library only).
"""

import base64
import json
import os
import socket
import struct
import time
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


def _open(page_prefix: str, port: int, timeout: float) -> Optional[socket.socket]:
    """A websocket to the page whose URL starts with `page_prefix`, or None."""
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
                sock.close()
                return None
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            sock.close()
            return None
        return sock
    except BaseException:
        sock.close()
        raise


def _call(sock: socket.socket, msg_id: int, method: str, params: Optional[dict] = None) -> dict:
    """Send one command and return its answer (events in between are skipped)."""
    _send(sock, json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    for _ in range(50):
        msg = json.loads(_recv_message(sock))
        if msg.get("id") == msg_id:
            return msg
    raise ConnectionError(f"no answer to {method}")


def capture(page_prefix: str, port: int = PORT, quality: int = 70, timeout: float = 5.0) -> Optional[bytes]:
    """JPEG bytes of the page whose URL starts with `page_prefix`, or None."""
    try:
        sock = _open(page_prefix, port, timeout)
        if sock is None:
            return None
        try:
            msg = _call(sock, 1, "Page.captureScreenshot", {"format": "jpeg", "quality": quality})
            data = (msg.get("result") or {}).get("data")
            return base64.b64decode(data) if data else None
        finally:
            sock.close()
    except (OSError, ValueError, ConnectionError, json.JSONDecodeError):
        return None


def fit_fullscreen(page_prefix: str, port: int = PORT, timeout: float = 5.0, settle: float = 0.5) -> Optional[str]:
    """Put the kiosk window back over the whole screen if it no longer covers it.

    A screen that goes away and comes back (a TV switched off and on, an HDMI
    replug, a new RDP session's virtual monitor) leaves Chromium saying
    "fullscreen" with its window at the old or a fallback size. Asking for
    fullscreen again does nothing then; going through "normal" refits it.

    Returns what was wrong ("1050x811 on a 1600x900 screen") when the window was
    refitted, None when it already covered the screen or nothing could be asked.
    """
    try:
        sock = _open(page_prefix, port, timeout)
        if sock is None:
            return None
        try:
            win = _call(sock, 1, "Browser.getWindowForTarget").get("result") or {}
            window_id, bounds = win.get("windowId"), win.get("bounds") or {}
            answer = _call(sock, 2, "Runtime.evaluate",
                           {"expression": "[screen.width, screen.height]", "returnByValue": True})
            size = ((answer.get("result") or {}).get("result") or {}).get("value") or [0, 0]
            screen_w, screen_h = int(size[0]), int(size[1])
            w, h = int(bounds.get("width", 0)), int(bounds.get("height", 0))
            if window_id is None or screen_w <= 0 or screen_h <= 0:
                return None                 # no screen to fit to
            if bounds.get("windowState") == "fullscreen" and w >= screen_w and h >= screen_h:
                return None
            _call(sock, 3, "Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}})
            time.sleep(settle)
            _call(sock, 4, "Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "fullscreen"}})
            return f"{w}x{h} on a {screen_w}x{screen_h} screen"
        finally:
            sock.close()
    except (OSError, ValueError, TypeError, IndexError, ConnectionError, json.JSONDecodeError):
        return None

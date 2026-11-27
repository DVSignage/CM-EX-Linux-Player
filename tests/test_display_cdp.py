"""Finding the desktop session, and the DevTools screenshot client."""

import base64
import json
import os
import socket
import socketserver
import struct
import threading
import time

from cmx import cdp, display


def test_xauthority_prefers_the_live_xwayland_file(tmp_path):
    run, home = tmp_path / "run", tmp_path / "home"
    run.mkdir()
    home.mkdir()
    (home / ".Xauthority").write_text("x")
    assert display.find_xauthority(1000, home, run_dir=str(run)) == str(home / ".Xauthority")
    old = run / ".mutter-Xwaylandauth.AAAA"
    old.write_text("x")
    os.utime(old, (1000, 1000))
    new = run / ".mutter-Xwaylandauth.BBBB"
    new.write_text("x")
    assert display.find_xauthority(1000, home, run_dir=str(run)) == str(new)     # newest login wins
    # a stale configured path (an old login's) is replaced by the live one
    assert display.find_xauthority(1000, home, current=str(old), run_dir=str(run)) == str(new)
    assert display.find_xauthority(1000, home, current=str(new), run_dir=str(run)) == str(new)


def test_x_socket_path():
    assert display.x_socket(":0") == "/tmp/.X11-unix/X0"
    assert display.x_socket(":1.0") == "/tmp/.X11-unix/X1"


def test_websocket_frames_round_trip():
    a, b = socket.socketpair()
    try:
        big = "x" * 70000                                  # 64-bit length frame
        for text in ("hello", "y" * 300, big):
            cdp._send(a, text)                             # masked, as a client must
            assert cdp._recv_message(b) == text
        # an unmasked, fragmented server message with a ping in between
        b.sendall(bytes([0x01, 3]) + b"abc" + bytes([0x89, 0]) + bytes([0x80, 3]) + b"def")
        assert cdp._recv_message(a) == "abcdef"
    finally:
        a.close()
        b.close()


class FakeDevTools(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def test_capture_against_a_fake_devtools_endpoint():
    jpeg = b"\xff\xd8\xff\xe0fakejpeg"

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            req = b""
            while b"\r\n\r\n" not in req:
                req += self.request.recv(1024)
            port = self.server.server_address[1]
            if req.startswith(b"GET /json"):
                body = json.dumps([{"type": "page", "url": "http://127.0.0.1:8083/",
                                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/1"}]).encode()
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                                     + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                return
            self.request.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            msg = json.loads(cdp._recv_message(self.request))
            assert msg["method"] == "Page.captureScreenshot"
            reply = json.dumps({"id": msg["id"], "result": {"data": base64.b64encode(jpeg).decode()}}).encode()
            self.request.sendall(bytes([0x81, 126]) + struct.pack(">H", len(reply)) + reply)

    srv = FakeDevTools(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert cdp.capture("http://127.0.0.1:8083/", port=srv.server_address[1]) == jpeg
        assert cdp.capture("http://elsewhere/", port=srv.server_address[1]) is None
    finally:
        srv.shutdown()
        srv.server_close()


def window_devtools(bounds, screen):
    """A fake DevTools page that answers the window and screen questions and
    records every method it is asked for (server.calls)."""

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            req = b""
            while b"\r\n\r\n" not in req:
                req += self.request.recv(1024)
            port = self.server.server_address[1]
            if req.startswith(b"GET /json"):
                body = json.dumps([{"type": "page", "url": "http://127.0.0.1:8090/",
                                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/1"}]).encode()
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                                     + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                return
            self.request.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            while True:
                try:
                    msg = json.loads(cdp._recv_message(self.request))
                except ConnectionError:
                    return
                self.server.calls.append((msg["method"], msg.get("params") or {}))
                if msg["method"] == "Browser.getWindowForTarget":
                    result = {"windowId": 7, "bounds": bounds}
                elif msg["method"] == "Runtime.evaluate":
                    result = {"result": {"type": "object", "value": screen}}
                else:
                    result = {}
                reply = json.dumps({"id": msg["id"], "result": result}).encode()
                self.request.sendall(bytes([0x81, len(reply)]) if len(reply) < 126 else
                                     bytes([0x81, 126]) + struct.pack(">H", len(reply)))
                self.request.sendall(reply)

    srv = FakeDevTools(("127.0.0.1", 0), Handler)
    srv.calls = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_fit_fullscreen_refits_a_window_left_smaller_than_the_screen():
    # A screen that went away and came back (TV power cycle, HDMI replug, a new
    # RDP session's virtual monitor): Chromium still says "fullscreen" but its
    # window kept the old size.
    srv = window_devtools({"left": 76, "top": 79, "width": 1050, "height": 811, "windowState": "fullscreen"},
                          [1600, 900])
    try:
        fixed = cdp.fit_fullscreen("http://127.0.0.1:8090/", port=srv.server_address[1], settle=0)
        assert fixed == "1050x811 on a 1600x900 screen"
        states = [p["bounds"]["windowState"] for m, p in srv.calls if m == "Browser.setWindowBounds"]
        assert states == ["normal", "fullscreen"]
        assert all(p.get("windowId") == 7 for m, p in srv.calls if m == "Browser.setWindowBounds")
    finally:
        srv.shutdown()
        srv.server_close()


def test_fit_fullscreen_leaves_a_window_that_covers_the_screen():
    srv = window_devtools({"left": 0, "top": 0, "width": 1920, "height": 1080, "windowState": "fullscreen"},
                          [1920, 1080])
    try:
        assert cdp.fit_fullscreen("http://127.0.0.1:8090/", port=srv.server_address[1], settle=0) is None
        assert not [m for m, _ in srv.calls if m == "Browser.setWindowBounds"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_fit_fullscreen_without_a_screen_size_does_nothing():
    # No monitor at all: nothing sensible to fit to.
    srv = window_devtools({"left": 0, "top": 0, "width": 500, "height": 50, "windowState": "fullscreen"}, [0, 0])
    try:
        assert cdp.fit_fullscreen("http://127.0.0.1:8090/", port=srv.server_address[1], settle=0) is None
        assert not [m for m, _ in srv.calls if m == "Browser.setWindowBounds"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_fit_fullscreen_without_chromium_is_none():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert cdp.fit_fullscreen("http://127.0.0.1:8090/", port=port, timeout=1, settle=0) is None


def test_capture_without_chromium_is_none():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    t = time.monotonic()
    assert cdp.capture("http://127.0.0.1:8083/", port=port, timeout=1) is None
    assert time.monotonic() - t < 3

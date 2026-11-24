"""NDI output: the on/name policy, the steady frame loop, the screencast
source, and the player's desired-state handling."""

import asyncio
import base64
import io
import json
import socketserver
import threading
import time

import httpx
import pytest
from PIL import Image

pytest.importorskip("numpy")   # NDI output is optional (requirements-ndi.txt)

from cmx import cdp, ndi_out
from cmx.config import Paths, Settings, save_state
from cmx.player import Player
from cmx.surface import PageSurface

from fake_cms import FakeCms


class FakeSender:
    def __init__(self, ok=True):
        self.ok = ok
        self.active = False
        self.source_name = ""
        self.frames = []
        self.starts = []

    def start(self, name):
        self.starts.append(name)
        if self.ok:
            self.active, self.source_name = True, name
        return self.ok

    def stop(self):
        self.active = False

    def send_frame(self, frame, fps=30):
        self.frames.append((frame.shape, fps))
        return True


class StillSource:
    def __init__(self):
        self.closed = False

    def grab(self):
        return Image.new("RGB", (64, 36), (10, 20, 30))

    def close(self):
        self.closed = True


def test_policy_matches_dvsi():
    assert ndi_out.decide(False, "Local", None, None) == (False, "Local")
    assert ndi_out.decide(False, "Local", False, "CMS") == (False, "Local")
    assert ndi_out.decide(True, "Local", False, "CMS") == (True, "Local")      # a CMS false does not kill a local output
    assert ndi_out.decide(False, "Local", True, "CMS") == (True, "CMS")
    assert ndi_out.decide(True, "Local", True, "  ") == (True, "Local")         # a blank CMS name keeps the local one


def test_to_bgra_order_and_downscale():
    img = Image.new("RGB", (4000, 2000), (255, 0, 0))
    arr = ndi_out.to_bgra(img, max_width=1920)
    assert arr.shape == (960, 1920, 4) and arr.flags["C_CONTIGUOUS"]
    assert tuple(arr[0, 0]) == (0, 0, 255, 255)                                 # red in BGRA


def test_output_sends_a_steady_stream_and_stops():
    sender, source = FakeSender(), StillSource()
    out = ndi_out.NdiOutput(sender, lambda: source, fps=20)
    ok, msg = out.start("Lobby")
    assert ok and msg == "broadcasting as 'Lobby'"
    time.sleep(0.35)
    assert out.frames_sent >= 4 and sender.frames[0] == ((36, 64, 4), 20)
    assert out.start("Lobby") == (True, "already broadcasting as 'Lobby'")
    assert sender.starts == ["Lobby"]
    out.stop()
    assert not sender.active and source.closed


def test_output_without_ndi_runtime():
    out = ndi_out.NdiOutput(FakeSender(ok=False), StillSource)
    ok, msg = out.start("X")
    assert not ok and "NDI" in msg
    assert ndi_out.NdiOutput(None, StillSource).start("X")[0] is False


def test_screencast_source_takes_and_acks_frames():
    jpeg = io.BytesIO()
    Image.new("RGB", (32, 18), (0, 255, 0)).save(jpeg, "JPEG")
    acks = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            req = b""
            while b"\r\n\r\n" not in req:
                req += self.request.recv(1024)
            port = self.server.server_address[1]
            if req.startswith(b"GET /json"):
                body = json.dumps([{"type": "page", "url": "http://127.0.0.1:8090/",
                                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/1"}]).encode()
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
                                     + b"\r\nConnection: close\r\n\r\n" + body)
                return
            self.request.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            start = json.loads(cdp._recv_message(self.request))
            assert start["method"] == "Page.startScreencast"
            event = json.dumps({"method": "Page.screencastFrame",
                                "params": {"data": base64.b64encode(jpeg.getvalue()).decode(), "sessionId": 7}}).encode()
            import struct
            self.request.sendall(bytes([0x81, 126]) + struct.pack(">H", len(event)) + event)
            acks.append(json.loads(cdp._recv_message(self.request)))
            time.sleep(1)

    class Srv(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Srv(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    src = ndi_out.ScreencastSource("http://127.0.0.1:8090/", port=srv.server_address[1])
    try:
        img = None
        for _ in range(40):
            img = src.grab()
            if img is not None:
                break
            time.sleep(0.05)
        assert img is not None and img.size == (32, 18)
        for _ in range(40):
            if acks:
                break
            time.sleep(0.05)
        assert acks and acks[0]["method"] == "Page.screencastFrameAck" and acks[0]["params"]["sessionId"] == 7
    finally:
        src.close()
        srv.shutdown()
        srv.server_close()


class RecordingServer:
    def send(self, msg):
        pass


def make_player(tmp_path, local_on=False, local_name=""):
    cms = FakeCms()
    paths = Paths(tmp_path / "state")
    paths.ensure()
    save_state(paths, {"player_id": cms.player_id, "player_secret": cms.secret, "cms_url": "http://cms"})
    p = Player(Settings(cms_url="http://cms", ndi_output=local_on, ndi_output_name=local_name), paths,
               transport=httpx.MockTransport(cms.handler), surface=PageSurface(RecordingServer()),
               enable_local_api=False)
    sender = FakeSender()
    p.ndi_output = ndi_out.NdiOutput(sender, StillSource, fps=10)
    return p, sender


def test_cms_desired_state_drives_the_output(tmp_path):
    async def go():
        p, sender = make_player(tmp_path)
        await p.handle_response({"command": "none", "ndi_broadcast_active": False})
        assert not sender.active                                            # nobody asked
        await p.handle_response({"command": "none", "ndi_broadcast_active": True, "ndi_broadcast_name": "Lobby"})
        assert sender.active and sender.source_name == "Lobby"
        await p.handle_response({"command": "none", "ndi_broadcast_active": True, "ndi_broadcast_name": "Hall"})
        assert sender.source_name == "Hall"                                 # a rename is a change
        await p.handle_response({"command": "none", "ndi_broadcast_active": False})
        assert not sender.active
        await p.handle_command({"command": "start_ndi_broadcast", "ndi_broadcast_name": "Cmd"})
        assert sender.active and sender.source_name == "Cmd"
        await p.handle_command({"command": "stop_ndi_broadcast"})
        assert not sender.active
        await p.shutdown()

    asyncio.run(go())


def test_cms_off_keeps_a_locally_enabled_output(tmp_path):
    async def go():
        p, sender = make_player(tmp_path, local_on=True, local_name="Kiosk A")
        p._apply_ndi_output()
        assert sender.active and sender.source_name == "Kiosk A"
        await p.handle_response({"command": "none", "ndi_broadcast_active": False})
        assert sender.active and sender.source_name == "Kiosk A"
        await p.handle_response({"command": "none", "ndi_broadcast_active": True, "ndi_broadcast_name": "CMS name"})
        assert sender.source_name == "CMS name"
        await p.handle_response({"command": "none", "ndi_broadcast_active": False})
        assert sender.active and sender.source_name == "Kiosk A"            # back to the local name, still on
        assert p.heartbeat_body()["ndi_broadcast_active"] is True
        await p.shutdown()

    asyncio.run(go())

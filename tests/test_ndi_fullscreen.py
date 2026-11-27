"""Full-screen NDI (show_ndi / show_ndi_wall) in the renderer page."""

import asyncio
import http.client
import io
import threading

import httpx
import pytest

from cmx import ndi_in
from cmx.config import Paths, Settings, save_state
from cmx.player import Player
from cmx.renderer.server import RendererServer
from cmx.surface import PageSurface

from fake_cms import FakeCms
from test_ndi_in import FakeBackend

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")


def halves():
    """640x360 BGRX: left half red, right half blue."""
    f = np.zeros((360, 640, 4), np.uint8)
    f[:, :320, 2] = 255
    f[:, 320:, 0] = 255
    return f


def rgb_at_centre(jpg):
    img = Image.open(io.BytesIO(jpg)).convert("RGB")
    return img.size, img.getpixel((img.size[0] // 2, img.size[1] // 2))


# ---- the crop -----------------------------------------------------------------

def test_parse_crop():
    assert ndi_in.parse_crop("0.5", "0", "0.5", "1") == (0.5, 0.0, 0.5, 1.0)
    assert ndi_in.parse_crop(0.75, 0, 0.5, 1) == (0.75, 0.0, 0.25, 1.0)          # clipped to the frame
    assert ndi_in.parse_crop(0, 0, 1, 1) is None                                  # the whole frame
    assert ndi_in.parse_crop(None, None, None, None) is None
    assert ndi_in.parse_crop("a", 0, 1, 1) is None and ndi_in.parse_crop(0, 0, 0, 1) is None


def test_crop_frame_cuts_the_region():
    f = halves()
    right = ndi_in.crop_frame(f, (0.5, 0.0, 0.5, 1.0))
    assert right.shape[:2] == (360, 320) and right[0, 0, 0] == 255 and right[0, 0, 2] == 0
    assert ndi_in.crop_frame(f, None) is f
    assert ndi_in.crop_frame(f, (0.999, 0.999, 0.001, 0.001)).shape[:2] == (1, 1)   # never empty


def test_stream_encodes_only_the_wall_region():
    be = FakeBackend(["S"])
    be.frame = halves()
    be.sending.set()
    feeds = ndi_in.NdiFeeds(be, linger=0)
    feed = feeds.acquire("S")
    stop = threading.Event()
    gen = feeds.frames(feed, 0, 0, stop, (0.5, 0.0, 0.5, 1.0))
    for _ in range(30):                                   # past the 640x360 waiting picture
        size, (r, g, b) = rgb_at_centre(next(gen))
        if size == (320, 360):
            break
    assert size == (320, 360) and b > 200 and r < 60     # the right half only
    stop.set()
    feeds.release(feed)
    feeds.close()


def test_route_passes_the_crop(tmp_path):
    be = FakeBackend(["S"])
    be.frame = halves()
    be.sending.set()
    for d in ("cache", "screens", "rss"):
        (tmp_path / d).mkdir()
    srv = RendererServer(tmp_path / "cache", tmp_path / "screens", tmp_path / "rss", lambda m: None, port=0)
    assert srv.start() is None
    srv.ndi_feeds = feeds = ndi_in.NdiFeeds(be, linger=0)
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        c.request("GET", "/ndi/stream?source=S&w=0&h=0&cx=0&cy=0&cw=0.5&ch=1")
        r = c.getresponse()
        seen = None
        for _ in range(30):
            assert r.fp.readline() == b"--ndiframe\r\n"
            r.fp.readline()
            n = int(r.fp.readline().split(b":")[1])
            r.fp.readline()
            seen = rgb_at_centre(r.fp.read(n))
            r.fp.readline()
            if seen[0] == (320, 360):
                break
        size, (red, g, b) = seen
        assert size == (320, 360) and red > 200 and b < 60                         # the left half only
        r.close()
        c.close()
    finally:
        srv.stop()
        feeds.close()


# ---- the player ----------------------------------------------------------------------

class RecordingServer:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def of(self, type_):
        return [m for m in self.sent if m.get("type") == type_]


def make(tmp_path, available=True):
    cms = FakeCms()
    paths = Paths(tmp_path / "state")
    paths.ensure()
    save_state(paths, {"player_id": cms.player_id, "player_secret": cms.secret, "cms_url": "http://cms",
                       "site_id": "s1", "connection_mode": "cloud"})
    server = RecordingServer()
    p = Player(Settings(cms_url="http://cms"), paths, transport=httpx.MockTransport(cms.handler),
               surface=PageSurface(server), enable_local_api=False)
    p.online = True
    p.ndi_feeds = ndi_in.NdiFeeds(FakeBackend(["CAM"]) if available else None)
    return p, server


def test_show_ndi_goes_to_the_page_and_is_reported(tmp_path):
    async def go():
        p, server = make(tmp_path)
        await p.handle_command({"command": "show_ndi", "command_id": "n1", "command_args": {"source_name": "STUDIO (CAM)"}})
        [m] = server.of("ndi")
        assert m["source"] == "STUDIO (CAM)" and m["crop"] is None
        body = p.heartbeat_body()
        assert body["ndi_receive_active"] is True and body["ndi_receive_source"] == "STUDIO (CAM)"
        await p.handle_command({"command": "show_ndi", "source_name": "STUDIO (CAM)"})
        assert len(server.of("ndi")) == 1                                        # same source: left alone
        stops = len(server.of("stop"))
        await p.handle_command({"command": "hide_ndi"})
        assert len(server.of("stop")) == stops + 1                               # the page closes its stream
        assert "ndi_receive_active" not in p.heartbeat_body()
        await p.shutdown()

    asyncio.run(go())


def test_show_ndi_wall_sends_this_screens_region(tmp_path):
    async def go():
        p, server = make(tmp_path)
        crop = {"x": 960, "y": 0, "w": 960, "h": 1080, "canvas_w": 1920, "canvas_h": 1080}
        await p.handle_command({"command": "show_ndi_wall", "source_name": "CAM", "wall_crop": crop})
        assert server.of("ndi")[-1]["crop"] == [0.5, 0.0, 0.5, 1.0]
        crop2 = dict(crop, x=0)
        await p.handle_command({"command": "show_ndi_wall", "source_name": "CAM", "wall_crop": crop2})
        assert server.of("ndi")[-1]["crop"] == [0.0, 0.0, 0.5, 1.0]                # a new region is a new show
        await p.handle_command({"command": "show_capture", "capture_device_label": "HDMI"})
        assert "ndi_receive_active" not in p.heartbeat_body()                      # the capture took the screen
        await p.shutdown()

    asyncio.run(go())


def test_without_the_ndi_runtime_the_screen_is_not_taken(tmp_path):
    async def go():
        p, server = make(tmp_path, available=False)
        await p.handle_command({"command": "show_ndi", "source_name": "CAM"})
        assert server.of("ndi") == [] and p._ndi_active_key is None
        await p.shutdown()

    asyncio.run(go())

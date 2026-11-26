"""NDI sources inside templates: the feed hub, the MJPEG route and the element."""

import asyncio
import http.client
import io
import threading
import time

import pytest

from cmx import ndi_in
from cmx.renderer.server import RendererServer
from cmx.templates import TemplateBuilder

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")


class FakeBackend:
    """A network where sources appear when the test says so."""

    def __init__(self, names=()):
        self.names = list(names)
        self.connected = []
        self.closed = []
        self.frame = np.zeros((360, 640, 4), np.uint8)
        self.frame[:, :, 2] = 255            # BGRX: red
        self.sending = threading.Event()
        self.shut = False

    def sources(self, wait_ms=1000):
        time.sleep(0.01)
        return list(self.names)

    def connect(self, name):
        if name not in self.names:
            return None
        self.connected.append(name)
        return {"name": name}

    def capture(self, recv, timeout_ms=200):
        if self.sending.wait(0.02):
            time.sleep(0.01)
            return self.frame
        return None

    def close(self, recv):
        self.closed.append(recv["name"])

    def shutdown(self):
        self.shut = True


def wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def jpeg_size(data):
    return Image.open(io.BytesIO(data)).size


# ---- readers ---------------------------------------------------------------

def test_source_aliases_and_matching():
    assert ndi_in.source_for({"sourceName": " CAM (OBS) "}) == "CAM (OBS)"
    assert ndi_in.source_for({"source_name": "A"}) == "A" and ndi_in.source_for({"ndiSource": "B"}) == "B"
    assert ndi_in.source_for({"sourceName": "  "}) is None
    names = ["STUDIO (Main)", "STUDIO (Backup)"]
    assert ndi_in.match_source("STUDIO (Backup)", names) == "STUDIO (Backup)"
    assert ndi_in.match_source("studio (main)", names) == "STUDIO (Main)"
    assert ndi_in.match_source("Backup", names) == "STUDIO (Backup)"
    assert ndi_in.match_source("Other", names) is None


def test_frames_scale_down_to_the_element_never_up():
    assert ndi_in.fit_size(1920, 1080, 480, 480) == (480, 270)
    assert ndi_in.fit_size(640, 360, 1920, 1080) == (640, 360)
    assert ndi_in.fit_size(3840, 2160, 0, 0) == (1920, 1080)          # capped at MAX_EDGE
    frame = np.zeros((360, 640, 4), np.uint8)
    frame[:, :, 2] = 255
    jpg = ndi_in.encode_jpeg(frame, 320, 320)
    img = Image.open(io.BytesIO(jpg)).convert("RGB")
    assert img.size == (320, 180)
    r, g, b = img.getpixel((160, 90))
    assert r > 200 and g < 60 and b < 60                               # BGRX read as BGR, not RGB


# ---- feeds -------------------------------------------------------------------

def test_waits_for_the_source_then_shares_one_receiver():
    be = FakeBackend()
    feeds = ndi_in.NdiFeeds(be, linger=0.2)
    a = feeds.acquire("CAM (OBS)")
    b = feeds.acquire("CAM (OBS)")
    assert a is b and a.users == 2
    time.sleep(0.1)
    assert a.state(time.monotonic()) == "waiting" and be.connected == []
    be.names.append("CAM (OBS)")                                        # the sender comes up
    assert wait(lambda: a.connected_as == "CAM (OBS)", 5)
    be.sending.set()
    assert wait(lambda: a.state(time.monotonic()) == "receiving")
    assert feeds.active() == [{"source": "CAM (OBS)", "connected_as": "CAM (OBS)", "state": "receiving", "viewers": 2}]
    feeds.release(a)
    feeds.release(b)
    assert wait(lambda: be.closed == ["CAM (OBS)"], 3)                  # stopped after the linger
    assert feeds.active() == [] and be.connected == ["CAM (OBS)"]
    feeds.close()
    assert be.shut


def test_reacquiring_within_the_linger_keeps_the_receiver():
    be = FakeBackend(["S"])
    feeds = ndi_in.NdiFeeds(be, linger=0.5)
    a = feeds.acquire("S")
    assert wait(lambda: a.connected_as == "S")
    feeds.release(a)
    time.sleep(0.1)
    b = feeds.acquire("S")                                              # a crossfade to the same source
    assert b is a
    time.sleep(0.7)
    assert be.connected == ["S"] and be.closed == []
    feeds.close()
    assert be.closed == ["S"]


def test_stream_shows_waiting_then_frames_scaled_to_the_element():
    be = FakeBackend(["S"])
    feeds = ndi_in.NdiFeeds(be, linger=0)
    feed = feeds.acquire("S")
    stop = threading.Event()
    gen = feeds.frames(feed, 320, 320, stop)
    first = next(gen)
    assert jpeg_size(first) == (640, 360)                               # the waiting picture
    be.sending.set()
    frame = next(gen)
    while jpeg_size(frame) == (640, 360):
        frame = next(gen)
    assert jpeg_size(frame) == (320, 180)
    stop.set()
    assert len(list(gen)) <= 1                                          # ends once stopped
    feeds.release(feed)
    feeds.close()


def test_without_ndi_nothing_is_received():
    feeds = ndi_in.NdiFeeds(None)
    assert not feeds.available and feeds.acquire("S") is None


# ---- the renderer route ----------------------------------------------------------

def start_server(tmp_path):
    for d in ("cache", "screens", "rss"):
        (tmp_path / d).mkdir()
    srv = RendererServer(tmp_path / "cache", tmp_path / "screens", tmp_path / "rss", lambda m: None, port=0)
    assert srv.start() is None
    return srv


def test_route_without_ndi_answers_one_picture(tmp_path):
    srv = start_server(tmp_path)
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        c.request("GET", "/ndi/stream?source=S")
        r = c.getresponse()
        assert r.status == 200 and r.getheader("Content-Type") == "image/jpeg"
        assert jpeg_size(r.read()) == (640, 360)
    finally:
        srv.stop()


def test_route_streams_mjpeg_and_releases_on_disconnect(tmp_path):
    be = FakeBackend(["CAM (OBS)"])
    be.sending.set()
    srv = start_server(tmp_path)
    srv.ndi_feeds = feeds = ndi_in.NdiFeeds(be, linger=0)
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        c.request("GET", "/ndi/stream?source=CAM%20(OBS)&w=200&h=200")
        r = c.getresponse()
        assert r.status == 200 and r.getheader("Content-Type").startswith("multipart/x-mixed-replace; boundary=ndiframe")
        sizes = []
        while (200, 112) not in sizes and len(sizes) < 20:
            assert r.fp.readline() == b"--ndiframe\r\n"
            assert r.fp.readline() == b"Content-Type: image/jpeg\r\n"
            n = int(r.fp.readline().split(b":")[1])
            assert r.fp.readline() == b"\r\n"
            sizes.append(jpeg_size(r.fp.read(n)))
            assert r.fp.readline() == b"\r\n"
        assert (200, 112) in sizes
        assert feeds.active()[0]["viewers"] == 1
        r.close()
        c.close()
        assert wait(lambda: be.closed == ["CAM (OBS)"], 5)               # the page closed it: released
    finally:
        srv.stop()
        feeds.close()


# ---- the template element -------------------------------------------------------------

def test_ndi_element_is_drawn_natively(tmp_path):
    async def fetch(tid):
        return None

    async def ensure(cid, role):
        return None

    d = {"width": 1920, "height": 1080, "elements": [
        {"id": "n1", "type": "ndi", "x": 0, "y": 0, "width": 960, "height": 540,
         "props": {"sourceName": "STUDIO (Main)", "objectFit": "cover"}},
        {"id": "n2", "type": "ndi", "props": {}}]}
    built = asyncio.run(TemplateBuilder(fetch, ensure).build("T", d))
    assert built is not None                                            # no fall back to the web render
    els = {e["id"]: e for e in built["payload"]["elements"]}
    assert els["n1"]["source"] == "STUDIO (Main)" and els["n2"]["source"] is None

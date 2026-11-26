"""The renderer server and the page surface."""

import asyncio
import http.client
import json
import threading

from cmx.items import TEMPLATE, VIDEO, Item
from cmx.renderer.server import RendererServer
from cmx.surface import PageSurface


class FakeServer:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


def start(tmp_path, events):
    (tmp_path / "cache").mkdir()
    (tmp_path / "screens").mkdir()
    (tmp_path / "rss").mkdir()
    srv = RendererServer(tmp_path / "cache", tmp_path / "screens", tmp_path / "rss", events.append, port=0)
    assert srv.start() is None
    return srv


def get(srv, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def test_serves_page_media_with_range_and_rss(tmp_path):
    events = []
    srv = start(tmp_path, events)
    try:
        status, _, body = get(srv, "/")
        assert status == 200 and b"renderer.js" in body
        status, _, _ = get(srv, "/static/template.js")
        assert status == 200
        (tmp_path / "cache" / "c1.mp4").write_bytes(b"0123456789")
        status, h, body = get(srv, "/media/c1.mp4", {"Range": "bytes=2-5"})
        assert status == 206 and body == b"2345" and h["Content-Range"] == "bytes 2-5/10"
        assert get(srv, "/media/../secret")[0] in (400, 404)
        (tmp_path / "rss" / "f1.json").write_text('{"items":[{"title":"x"}]}')
        assert json.loads(get(srv, "/api/rss/f1")[2])["items"][0]["title"] == "x"
    finally:
        srv.stop()


def test_events_post_and_sse_replay(tmp_path):
    events = []
    srv = start(tmp_path, events)
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        c.request("POST", "/api/event", body=json.dumps({"type": "ended", "gen": 3}),
                  headers={"Content-Type": "application/json"})
        assert c.getresponse().status == 200
        assert events == [{"type": "ended", "gen": 3}]

        srv.send({"type": "volume", "volume": 40, "muted": False})
        srv.send({"type": "media", "gen": 1, "url": "/media/a.mp4"})
        lines = []

        def read():
            s = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            s.request("GET", "/events")
            r = s.getresponse()
            while len(lines) < 2:
                line = r.fp.readline().decode().strip()
                if line.startswith("data:"):
                    lines.append(json.loads(line[5:]))

        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(5)
        assert [m["type"] for m in lines] == ["volume", "media"]     # a reconnecting page gets the state back
    finally:
        srv.stop()


def test_page_surface_media_message_and_status(tmp_path):
    server = FakeServer()
    s = PageSurface(server)
    ended = []
    s.set_eof_callback(ended.append)
    a = Item(kind=VIDEO, content_id="a", name="a.mp4", path=tmp_path / "a.mp4", clip_start=5, clip_end=20)
    b = Item(kind=VIDEO, content_id="b", name="b.mp3", path=tmp_path / "b.mp3")
    gen = asyncio.run(s.show_media(a, False, loop=False, crop=(0.5, 0, 0.5, 1), preload=b))
    m = server.sent[-1]
    assert m["type"] == "media" and m["gen"] == gen and m["url"] == "/media/a.mp4"
    assert (m["start"], m["end"], m["crop"]) == (5, 20, [0.5, 0, 0.5, 1])
    assert m["preload"] == {"url": "/media/b.mp3", "kind": "video", "start": None}
    asyncio.run(s.show_media(b, False, loop=True, crop=None))
    assert server.sent[-1]["kind"] == "audio"            # the Now Playing card
    s.on_page_event({"type": "status", "gen": s.gen, "position": 3.5, "duration": 60})
    assert s.time_pos == 3.5 and s.duration == 60
    s.on_page_event({"type": "status", "gen": s.gen - 1, "position": 9})
    assert s.time_pos is None
    s.on_page_event({"type": "ended", "gen": 7})
    assert ended == [7]


def test_page_surface_template_web_and_screens(tmp_path):
    server = FakeServer()
    s = PageSurface(server)
    t = Item(kind=TEMPLATE, content_id="t", name="Menu", url="http://cms/api/v1/templates/T/render", template_id="T")
    t.extra.update(definition={"elements": []}, page={"tid": "T", "width": 1920, "height": 1080, "elements": [], "media": {}})
    assert asyncio.run(s.show_page(t))
    assert server.sent[-1]["type"] == "template" and server.sent[-1]["tid"] == "T"
    assert s.showing_page(t) and s.template_on_screen is t
    fallback = Item(kind=TEMPLATE, content_id="u", name="X", url="http://cms/api/v1/templates/U/render", canvas=(3840, 2160))
    asyncio.run(s.show_page(fallback))
    assert server.sent[-1]["type"] == "web" and server.sent[-1]["canvas"] == [3840, 2160]
    s.show_screen("enrollment", code="ABC", server="http://cms", site=None, link="dvsiconnect://enroll?v=1&code=ABC")
    m = server.sent[-1]
    assert m["name"] == "enrollment" and m["qr"].startswith("data:image/png;base64,")


def test_taken_port_moves_up_instead_of_failing(tmp_path):
    """Another program on 8090 (a kiosk service, a camera bridge) must not leave
    the screen showing its answers: the page takes the next free port."""
    import socket
    from cmx.renderer import server as rs

    for d in ("cache", "screens", "rss"):
        (tmp_path / d).mkdir()
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    taken = holder.getsockname()[1]
    srv = RendererServer(tmp_path / "cache", tmp_path / "screens", tmp_path / "rss", lambda e: None, port=taken)
    try:
        assert srv.start() is None
        assert taken < srv.port <= taken + rs.FALLBACK_PORTS
        assert srv.url == f"http://127.0.0.1:{srv.port}/"
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        conn.request("GET", "/")
        assert conn.getresponse().status == 200
    finally:
        srv.stop()
        holder.close()

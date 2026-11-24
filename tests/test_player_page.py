"""The orchestrator on the renderer-page path: templates, walls, patch,
edge mode, proof of play — against an in-memory CMS and a recording page."""

import asyncio
import json
import time

import httpx

from cmx.config import Paths, Settings, save_state
from cmx.edge import CONTROL
from cmx.items import IMAGE, Item
from cmx.player import Player
from cmx.surface import PageSurface

from conftest import MP4_HEAD, PNG_HEAD
from fake_cms import FakeCms


class RecordingServer:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def last(self, type_, **match):
        for m in reversed(self.sent):
            if m.get("type") == type_ and all(m.get(k) == v for k, v in match.items()):
                return m
        return None


TEMPLATE = {
    "width": 1920, "height": 1080,
    "elements": [
        {"id": "t1", "type": "text", "x": 0, "y": 0, "width": 400, "height": 100, "props": {"content": "Hello"}},
        {"id": "i1", "type": "image", "x": 0, "y": 200, "width": 400, "height": 300, "props": {"contentId": "img"}},
        {"id": "z1", "type": "trigger_zone", "x": 600, "y": 0, "width": 400, "height": 400, "props": {"triggerId": 1}},
        {"id": "r1", "type": "rss", "x": 0, "y": 900, "width": 1920, "height": 100, "props": {"contentId": "feed", "mode": "ticker"}},
    ],
    "interaction": {"policy": {"touch": True}, "rootSceneId": "home",
                    "scenes": [{"id": "home", "name": "Home",
                                "mappings": [{"triggerId": 1, "contentId": "wallclip"}]}]},
}


def make(tmp_path, cms, edge=False, transport=None):
    paths = Paths(tmp_path / "state")
    paths.ensure()
    save_state(paths, {"player_id": cms.player_id, "player_secret": cms.secret, "cms_url": "http://cms"})
    settings = Settings(cms_url="http://cms", connection_mode="edge" if edge else "",
                        edge_url="http://edge:8080" if edge else "")
    server = RecordingServer()
    p = Player(settings, paths, transport=transport or httpx.MockTransport(cms.handler),
               surface=PageSurface(server), enable_local_api=False)
    p.online = True
    return p, server


def template_cms():
    cms = FakeCms()
    cms.media = {"img": PNG_HEAD, "wallclip": MP4_HEAD + b"wall"}
    cms.meta = {"img": {"filename": "logo.png"}, "wallclip": {"filename": "wall.mp4"}}
    cms.templates = {"T": TEMPLATE}
    cms.rss = {"feed": [{"title": "Headline", "published": "2026-08-14T10:00:00+02:00"}]}
    cms.playlist = {"items": [{"content_id": "tc", "duration": 0,
                               "content": {"type": "template", "url": "http://cms/api/v1/templates/T/render"}}]}
    return cms


def test_native_template_wall_trigger_and_patch(tmp_path):
    cms = template_cms()

    async def go():
        p, server = make(tmp_path, cms)
        assert await p.fetch_playlist()
        shown = server.last("template")
        assert shown and shown["tid"] == "T"
        assert set(shown["media"]) == {"img", "wallclip"}
        assert (p.paths.cache_dir / "img.png").exists() and (p.paths.cache_dir / "wallclip.mp4").exists()
        assert p.wall is not None and p._rss_feeds == {"feed"}

        await p.handle_command({"command": "trigger", "command_id": "k1", "command_args": {"id": 1}})
        assert cms.acks[-1]["message"] == "trigger 1: playing wallclip (scene 'Home')"
        play = server.last("wall", op="play")
        assert play["surface"] == "zone:1" and play["url"] == "/media/wallclip.mp4"
        assert p.heartbeat_body()["scene_id"] == "home"

        p._page_event({"type": "zone_playing", "surface": "zone:1"})
        assert p.wall_host.is_playing(p.wall_host.expand(p.wall_host._key_of({"surface_key": "zone:1"}))[0])
        p._page_event({"type": "zone_returned", "surface": "zone:1"})
        assert not p.wall_host.any_zone_busy

        await asyncio.sleep(0.35)                                   # past the 300 ms debounce
        p._page_event({"type": "tap", "trigger_id": 1})            # touch is on for this wall
        assert server.last("wall", op="play") is not play

        await p.handle_command({"command": "template_patch", "command_id": "k2",
                                "command_args": {"template_id": "T", "element_id": "t1", "kind": "text", "text": "Bye"}})
        assert cms.acks[-1] == {"success": True, "message": "patched t1", "command_id": "k2", "command": "template_patch"}
        patch = server.last("template_patch")
        assert patch["element"]["props"]["content"] == "Bye"
        assert json.loads((p.paths.cache_dir / "template_T.json").read_text())["elements"][0]["props"]["content"] == "Bye"

        await p.handle_command({"command": "template_patch", "command_id": "k3",
                                "command_args": {"template_id": "OTHER", "element_id": "t1", "kind": "text"}})
        assert cms.acks[-1]["message"] == "template OTHER is not on screen"
        await p.handle_command({"command": "set_scene", "command_id": "k4", "command_args": {"scene_id": "nope"}})
        assert cms.acks[-1]["message"] == "unknown scene 'nope'"
        await p.shutdown()

    asyncio.run(go())


def test_edited_template_merges_in_place(tmp_path):
    cms = template_cms()

    async def go():
        p, server = make(tmp_path, cms)
        await p.fetch_playlist()
        first = server.last("template")
        edited = json.loads(json.dumps(TEMPLATE))
        edited["elements"][0]["props"]["content"] = "Edited"
        cms.templates["T"] = edited
        await p.process_playlist(cms.playlist)
        merge = server.last("template_merge")
        assert merge is not None and merge["elements"][0]["props"]["content"] == "Edited"
        assert server.last("template") is first           # not rebuilt from scratch
        await p.shutdown()

    asyncio.run(go())


def test_proof_of_play_and_health_in_edge_mode(tmp_path):
    cms = FakeCms()

    async def go():
        p, server = make(tmp_path, cms, edge=True)
        assert p.edge_mode
        a = Item(kind=IMAGE, content_id="a", name="a.png")
        b = Item(kind=IMAGE, content_id="b", name="b.png")
        p._playlist_uuid = "pl-1"
        p._on_item(a, None, "interrupted")
        time.sleep(0.3)
        p._on_item(b, a, "completed")
        pop = [e for e in p.telemetry.events if e["type"] == "proof_of_play"]
        assert len(pop) == 1 and pop[0]["asset_uuid"] == "a" and pop[0]["completed"] is True
        assert pop[0]["duration_ms"] >= 250 and pop[0]["playlist_uuid"] == "pl-1"
        p._on_item(None, b, "interrupted")                    # a flick under 250 ms is not reported
        assert len([e for e in p.telemetry.events if e["type"] == "proof_of_play"]) == 1
        h = p.health_event()
        assert h["type"] == "player_health" and "error" in h and h["player_version"]
        assert "temp_c" not in h or h["temp_c"] is not None
        await p._flush_telemetry()
        assert {e["type"] for e in cms.telemetry} >= {"proof_of_play"}
        await p.shutdown()

    asyncio.run(go())


def test_cloud_mode_holds_proof_of_play(tmp_path):
    cms = FakeCms()

    async def go():
        p, _ = make(tmp_path, cms)
        p.telemetry.enqueue({"event_id": "x", "type": "proof_of_play"})
        await p._flush_telemetry()
        assert cms.telemetry == [] and len(p.telemetry.events) == 1
        await p.shutdown()

    asyncio.run(go())


def test_edge_404_confirmed_against_cloud(tmp_path):
    cms = FakeCms()

    def handler(req):
        if req.url.host == "edge" and req.url.path.endswith("/heartbeat"):
            return httpx.Response(404)
        return cms.handler(req)

    async def go():
        p, _ = make(tmp_path, cms, edge=True, transport=httpx.MockTransport(handler))
        await p.beat()
        assert p.player_id == "P1"                                # not wiped on a stale edge
        assert p.cms.router.failed[CONTROL]
        assert any(e.get("code") == "edge_stale_404" for e in p.telemetry.events)
        cms.registered = False
        p.cms.router.failed[CONTROL] = False
        p.cms.router._hold_until.clear()
        p.enroll = lambda: asyncio.sleep(0)
        await p.beat()                                            # the cloud says gone too
        assert p.player_id is None
        await p.shutdown()

    asyncio.run(go())


def test_setup_screen_saves_connection(tmp_path):
    cms = FakeCms()

    async def go():
        paths = Paths(tmp_path / "state")
        server = RecordingServer()
        p = Player(Settings(), paths, transport=httpx.MockTransport(cms.handler),
                   surface=PageSurface(server), enable_local_api=False)
        p.enroll = lambda: asyncio.sleep(0)
        await p._on_setup({"mode": "edge", "cms_url": "https://cloud.example", "edge_url": "http://10.0.0.5:8080"})
        state = json.loads(paths.state.read_text())
        assert state["cms_url"] == "https://cloud.example" and state["connection_mode"] == "edge"
        assert p.edge_mode and p.cms.router.edge_url == "http://10.0.0.5:8080"
        await p._on_setup({"mode": "cloud", "cms_url": "nope"})
        assert server.last("screen", name="setup")["error"]
        await p.shutdown()

    asyncio.run(go())


def test_show_capture_suspends_playlist_and_reports_devices(tmp_path):
    cms = FakeCms()

    async def go():
        p, server = make(tmp_path, cms)
        await p.handle_command({"command": "show_capture", "command_id": "c1",
                                "command_args": {"capture_device_label": "USB Cam", "audio_enabled": "true"}})
        cap = server.last("capture")
        assert cap["device_label"] == "USB Cam" and cap["audio"] is True
        assert cms.acks[-1]["message"] == "show_capture accepted"
        assert p.heartbeat_body()["status"] == "playing"
        p._page_event({"type": "devices", "capture": [{"label": "USB Cam", "deviceId": "abc"}]})
        assert p.heartbeat_body()["capture_devices"] == [{"label": "USB Cam", "deviceId": "abc"}]
        await p.handle_command({"command": "hide_capture"})
        assert p._live_key is None
        await p.shutdown()

    asyncio.run(go())

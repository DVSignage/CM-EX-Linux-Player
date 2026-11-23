"""The orchestrator end to end against an in-memory CMS (no mpv, no browser)."""

import asyncio
import json

import httpx

import cmx.player as player_mod
from cmx.config import Paths, Settings, save_state
from cmx.player import Player

from conftest import MP4_HEAD, PNG_HEAD, FakeBrowser, FakeMpv
from fake_cms import FakeCms


def make(tmp_path, cms: FakeCms, enrolled=True):
    paths = Paths(tmp_path / "state")
    paths.ensure()
    if enrolled:
        save_state(paths, {"player_id": cms.player_id, "player_secret": cms.secret,
                           "cms_url": "http://cms"})
    settings = Settings(cms_url="http://cms")
    p = Player(settings, paths, transport=httpx.MockTransport(cms.handler),
               mpv=FakeMpv(), browser=FakeBrowser(), enable_local_api=False)
    return p


def playlist(cms: FakeCms):
    cms.media = {"v1": MP4_HEAD + b"video-one", "i1": PNG_HEAD}
    cms.playlist = {"items": [
        {"content_id": "v1", "duration": 0, "content": {"filename": "promo.mp4", "duration": 30}},
        {"content_id": "i1", "duration": 5, "content": {"filename": "promo.png"}},
        {"content_id": "t1", "duration": 20,
         "content": {"type": "template", "url": "http://cms/api/v1/templates/TT/render", "name": "Menu"}},
    ]}


async def settle(p: Player):
    if p._fetch_task:
        await p._fetch_task
    await asyncio.sleep(0)


def test_load_playlist_downloads_by_content_id_and_plays(tmp_path):
    cms = FakeCms()
    playlist(cms)

    async def go():
        p = make(tmp_path, cms)
        p.online = True
        await p.handle_response({"command": "load_playlist", "playlist_hash": "h1"})
        await settle(p)
        names = sorted(f.name for f in p.paths.cache_dir.iterdir())
        assert "v1.mp4" in names and "i1.png" in names and "template_TT.json" in names
        assert p.engine.has_content and p.engine.index == 0
        assert p.mpv.loads[-1][0].endswith("v1.mp4")
        assert p.engine.items[2].canvas == (3840, 2160)
        assert json.loads(p.paths.offline_playlist.read_text())["items"][0]["content_id"] == "v1"
        # the same hash again is skipped
        fetches = len(p.mpv.loads)
        await p.handle_response({"command": "load_playlist", "playlist_hash": "h1"})
        await settle(p)
        assert len(p.mpv.loads) == fetches
        assert all(s == cms.secret for s in cms.secrets_seen)
        await p.shutdown()

    asyncio.run(go())


def test_enveloped_command_runs_once_and_every_delivery_is_acked(tmp_path):
    cms = FakeCms()
    playlist(cms)

    async def go():
        p = make(tmp_path, cms)
        p.online = True
        await p.fetch_playlist()
        p.mpv.time_pos, p.mpv.duration = 3.0, 30.0
        cmd = {"command": "seek", "command_id": "k1", "command_args": {"position": 10}}
        await p.handle_command(cmd)
        await p.handle_command(cmd)            # re-delivered by the next beat
        assert p.mpv.seeks == [10.0]
        assert [a["command_id"] for a in cms.acks] == ["k1", "k1"]
        assert cms.acks[0] == {"success": True, "message": "seeked to 10s of 30s",
                               "command_id": "k1", "command": "seek"}
        await p.shutdown()

    asyncio.run(go())


def test_unknown_and_state_commands_are_acked(tmp_path):
    cms = FakeCms()

    async def go():
        p = make(tmp_path, cms)
        await p.handle_command({"command": "trigger", "command_id": "u1", "command_args": {"id": 3}})
        await p.handle_command({"command": "set_volume", "command_id": "v1",
                                "command_args": {"volume": "35", "muted": "false"}})
        await p.handle_command({"command": "show_ndi", "command_id": "n1"})
        await p.handle_command({"command": "trigger"})          # no id: no ack
        assert cms.acks[0]["success"] is False
        assert cms.acks[0]["message"] == "command 'trigger' not supported by this player"
        assert cms.acks[1]["message"] == "volume 35"
        assert p.mpv.volume == (35, False)
        assert json.loads(p.paths.state.read_text())["volume"] == 35
        assert cms.acks[2] == {"success": False, "message": "show_ndi needs 'source_name'",
                               "command_id": "n1", "command": "show_ndi"}
        assert len(cms.acks) == 3
        await p.shutdown()

    asyncio.run(go())


def test_refresh_reports_real_outcome_and_guards_reentry(tmp_path):
    cms = FakeCms()
    playlist(cms)

    async def go():
        p = make(tmp_path, cms)
        p.online = True
        await p.fetch_playlist()
        await p.handle_command({"command": "refresh", "command_id": "r1"})
        assert cms.acks[-1]["message"] == "Refresh completed" and cms.acks[-1]["success"]
        p._refreshing = True
        await p.handle_command({"command": "refresh", "command_id": "r2"})
        assert cms.acks[-1] == {"success": False, "message": "refresh already in progress",
                                "command_id": "r2", "command": "refresh"}
        p._refreshing = False
        cms.playlist = None
        await p.handle_command({"command": "refresh", "command_id": "r3"})
        assert cms.acks[-1]["success"] is False
        assert cms.acks[-1]["message"] == "no playlist came back — restored the cached playlist"
        assert p.engine.has_content
        await p.shutdown()

    asyncio.run(go())


def test_heartbeat_body_and_404_re_enrolls(tmp_path, monkeypatch):
    cms = FakeCms()
    playlist(cms)

    async def go():
        p = make(tmp_path, cms)
        p.online = True
        await p.fetch_playlist()
        await p.beat()
        body = cms.beats[-1]
        for key in ("status", "playback_status", "position", "duration", "current_content_id",
                    "playlist_index", "playlist_total", "scene_id", "home_scene_id", "error",
                    "capture_devices", "decklink_devices", "ndi_sources", "api_port", "player_version"):
            assert key in body, key
        assert body["current_content_id"] == "v1" and body["api_port"] == 8081
        cms.registered = False
        monkeypatch.setattr(p, "enroll", lambda: asyncio.sleep(0))
        await p.beat()
        assert p.player_id is None
        assert not p.paths.offline_playlist.exists()
        assert list(p.paths.cache_dir.iterdir()) == []
        assert not p.engine.has_content
        await p.shutdown()

    asyncio.run(go())


def test_enrollment_saves_secret_and_uses_it(tmp_path, monkeypatch):
    cms = FakeCms()
    cms.approved = True
    monkeypatch.setattr(player_mod, "POLL_INTERVAL", 0)

    async def go():
        p = make(tmp_path, cms, enrolled=False)
        task = asyncio.ensure_future(p.enroll())
        await asyncio.wait_for(task, 5)
        state = json.loads(p.paths.state.read_text())
        assert state["player_id"] == "P1" and state["player_secret"] == "s3cret"
        assert state["site_name"] == "Lobby"
        await p.beat()
        assert cms.secrets_seen[-1] == "s3cret"
        await p.shutdown()

    asyncio.run(go())


def test_restart_service_acks_then_stops(tmp_path):
    cms = FakeCms()

    async def go():
        p = make(tmp_path, cms)
        await p.handle_command({"command": "restart_service", "command_id": "x1"})
        assert cms.acks[-1]["message"] == "Restart acknowledged"
        assert p._stop.is_set() and p.exit_code == 0
        await p.shutdown()

    asyncio.run(go())


def test_wall_content_crops_and_desired_state(tmp_path):
    cms = FakeCms()
    cms.media = {"w1": MP4_HEAD}
    cms.meta = {"w1": {"type": "video", "filename": "wall.mp4"}}

    async def go():
        p = make(tmp_path, cms)
        p.online = True
        crop = {"x": 960, "y": 0, "w": 960, "h": 1080, "canvas_w": 1920, "canvas_h": 1080}
        await p.handle_command({"command": "load_wall_content", "wall_content_id": "w1", "wall_crop": crop})
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert p.engine.crop == (0.5, 0.0, 0.5, 1.0)
        assert p.mpv.loads[-1][1]["vf"].startswith("crop=w=iw*0.5")
        await p.handle_response({"command": "none", "desired_blank": True, "audio_volume": 20})
        assert p.engine.mode == "stopped" and p.mpv.volume == (20, False)
        await p.handle_response({"command": "none", "desired_blank": False})
        assert p.engine.mode == "playing"
        await p.shutdown()

    asyncio.run(go())


def test_boot_telemetry_is_sent(tmp_path):
    cms = FakeCms()

    async def go():
        p = make(tmp_path, cms)
        p._queue_stability_events()
        await p._flush_telemetry()
        assert [e["code"] for e in cms.telemetry] == ["boot"]
        assert cms.telemetry[0]["player_uuid"] == "P1"
        await p.shutdown()

    asyncio.run(go())

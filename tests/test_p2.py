"""P2 items: A/V trim, port-conflict telemetry, inventory refusal backoff,
reset registration."""

import asyncio
import http.client
import json

import httpx

from cmx import faults as F
from cmx import inventory as inv
from cmx.config import Paths, Settings, save_state
from cmx.health import port_conflict_message
from cmx.lan_api import LanApiServer
from cmx.player import Player
from cmx.surface import PageSurface
from cmx.telemetry import TelemetryQueue
from cmx.templates import av_offset

from fake_cms import FakeCms


class RecordingServer:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def last(self, type_):
        return next((m for m in reversed(self.sent) if m.get("type") == type_), None)


def make(tmp_path, settings=None, enrolled=True):
    cms = FakeCms()
    paths = Paths(tmp_path / "state")
    paths.ensure()
    if enrolled:
        save_state(paths, {"player_id": cms.player_id, "player_secret": cms.secret, "cms_url": "http://cms",
                           "site_id": "s1", "connection_mode": "cloud"})
    server = RecordingServer()
    p = Player(settings or Settings(cms_url="http://cms"), paths, transport=httpx.MockTransport(cms.handler),
               surface=PageSurface(server), enable_local_api=False)
    p.online = True
    return p, server, cms


# ---- A/V trim ------------------------------------------------------------------

def test_av_offset_aliases_and_clamp():
    assert av_offset({}) == 0
    assert av_offset({"av_offset_ms": "120"}) == 120
    assert av_offset({"avSyncMs": -900}) == -500
    assert av_offset({"audio_delay_ms": 750.4}) == 500
    assert av_offset({"av_offset_ms": True, "av_sync_ms": 40}) == 40


def test_capture_trim_applies_live_without_reopening(tmp_path):
    async def go():
        p, server, _ = make(tmp_path)
        await p.handle_command({"command": "show_capture", "capture_device_label": "HDMI", "av_offset_ms": 80})
        cap = server.last("capture")
        assert cap["av_offset_ms"] == 80
        await p.handle_command({"command": "show_capture", "capture_device_label": "HDMI", "av_offset_ms": -40})
        assert server.last("capture") is cap                          # same device: not reopened
        assert server.last("capture_trim")["av_offset_ms"] == -40
        p._page_event({"type": "capture_trim", "av_offset_ms": -40, "av_hold_ms": 0})
        body = p.heartbeat_body()
        assert (body["av_offset_ms"], body["av_hold_ms"]) == (-40, 0)
        await p.handle_command({"command": "hide_capture"})
        assert p.heartbeat_body()["av_offset_ms"] is None
        await p.shutdown()

    asyncio.run(go())


# ---- port conflicts --------------------------------------------------------------

def test_port_conflict_message_wording():
    assert port_conflict_message("control API", 8083, None) == \
        "port conflict: control API tcp port 8083 is held by another program"
    assert port_conflict_message("local API", 8081, "nginx").endswith("is held by nginx")


def test_port_conflict_fault_telemetry_and_clear(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr("cmx.player.port_holder", lambda port: "nginx")
        p, _, _ = make(tmp_path)
        p.faults.hold = 0
        p._bound("control API", 8083, "Address already in use")
        assert p.compute_error() == "port conflict: control API tcp port 8083 is held by nginx"
        events = [e for e in p.telemetry.events if e.get("code") == "port_conflict"]
        assert len(events) == 1 and events[0]["detail"]["port"] == 8083 and events[0]["detail"]["holder"] == "nginx"
        p._bound("control API", 8083, "Address already in use")       # still held: reported once
        assert len([e for e in p.telemetry.events if e.get("code") == "port_conflict"]) == 1
        p._bound("control API", 8083, None)                           # came free
        assert not p.faults.has(F.PORT)
        assert [e["code"] for e in p.telemetry.events if e.get("code", "").startswith("port_conflict")] == \
            ["port_conflict", "port_conflict_cleared"]
        await p.shutdown()

    asyncio.run(go())


def test_port_conflict_before_enrollment_is_reported_later(tmp_path):
    async def go():
        p, _, _ = make(tmp_path, enrolled=False)
        p._bound("renderer page", 8090, "in use")
        assert not [e for e in p.telemetry.events if e.get("code") == "port_conflict"]
        p.state["player_id"] = "P1"
        p._queue_stability_events()
        assert [e for e in p.telemetry.events if e.get("code") == "port_conflict"]
        await p.shutdown()

    asyncio.run(go())


# ---- inventory refusal backoff ------------------------------------------------------

def test_refused_inventory_reports_daily_only_until_the_version_changes(tmp_path):
    now = [1_000_000.0]
    r = inv.InventoryReporter(tmp_path / "inv.json", clock=lambda: now[0])
    body = {"agent": {"version": "2.3.0"}, "apps_hash": "a"}
    r.sent(body)
    r.refused("2.3.0")
    now[0] += inv.MIN_GAP + 1
    assert not r.due({"agent": {"version": "2.3.0"}, "apps_hash": "CHANGED"})     # daily only now
    assert r.due({"agent": {"version": "2.4.0"}, "apps_hash": "a"})               # a new player version
    now[0] += inv.FULL_EVERY
    assert r.due(body)
    r.sent(body)
    assert r.state["refused_version"] == "2.3.0"


def test_queue_reports_a_refused_event(tmp_path):
    dropped = []
    q = TelemetryQueue(tmp_path / "q.json", routable={"inventory"}, on_drop=dropped.append)
    q.enqueue({"event_id": "i1", "type": "inventory"})

    async def send(batch):
        return httpx.Response(422)

    asyncio.run(q.flush(send))
    assert [e["event_id"] for e in dropped] == ["i1"] and q.events == []


def test_player_switches_inventory_to_daily_on_refusal(tmp_path):
    async def go():
        p, _, _ = make(tmp_path)
        p._telemetry_dropped({"type": "inventory", "agent": {"version": "2.3.0"}})
        assert p.inventory.state["refused_version"] == "2.3.0"
        await p.shutdown()

    asyncio.run(go())


# ---- reset registration -----------------------------------------------------------------

def test_reset_registration_returns_to_setup(tmp_path):
    async def go():
        p, server, _ = make(tmp_path, settings=Settings())          # address came from the setup screen
        (p.paths.cache_dir / "c1.mp4").write_bytes(b"x")
        p.paths.offline_playlist.write_text("{}")
        msg = await p.reset_registration("test")
        assert msg == "registration reset — showing the setup screen"
        state = json.loads(p.paths.state.read_text())
        for k in ("player_id", "player_secret", "site_id", "cms_url"):
            assert k not in state
        assert state["connection_mode"] == "cloud"                    # the mode is kept
        assert list(p.paths.cache_dir.iterdir()) == [] and not p.paths.offline_playlist.exists()
        assert server.last("screen")["name"] == "setup"
        await p.shutdown()

    asyncio.run(go())


def test_reset_registration_with_a_configured_address_shows_a_new_code(tmp_path, monkeypatch):
    async def go():
        p, _, _ = make(tmp_path)
        started = []
        monkeypatch.setattr(p, "enroll", lambda: started.append(1) or asyncio.sleep(0))
        msg = await p.reset_registration("test")
        assert msg == "registration reset — showing a new code" and started == [1]
        assert p.player_id is None and p.cms_url == "http://cms"
        await p.shutdown()

    asyncio.run(go())


def test_reset_route_needs_the_local_key():
    calls = []
    srv = LanApiServer("K", lan_enabled=False, trigger_key="", handle_wall=lambda c, a: (True, "", None),
                       snapshot=dict, control=lambda n: None, device_info=dict, capture_devices=list,
                       ndi_sources=dict, preview_jpeg=lambda: None,
                       reset_registration=lambda: calls.append(1) or "registration reset", port=0)
    srv.start()
    try:
        def post(headers):
            c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            c.request("POST", "/api/reset-registration", headers=headers)
            r = c.getresponse()
            return r.status, r.read()
        assert post({})[0] == 401 and calls == []
        status, body = post({"X-Local-Key": "K"})
        assert status == 200 and json.loads(body)["message"] == "registration reset" and calls == [1]
    finally:
        srv.stop()

"""The control API on 8083: DVSI Plus LocalApiPolicy's cases, and the routes."""

import http.client
import json

import pytest

from cmx.lan_api import (ALLOW, FORBIDDEN, NOT_FOUND, UNAUTHORIZED, Decision, LanApiServer, decide,
                         wall_command)


def D(path, method="GET", local=True, origin=False, wall_key_set=False, wall_key_ok=True, local_key_ok=False):
    return decide(path, method, local, origin, wall_key_set, wall_key_ok, local_key_ok)


# ---- policy (LocalApiPolicyTests.cs, case for case) --------------------------

@pytest.mark.parametrize("path,method", [
    ("/api/play", "POST"), ("/api/pause", "POST"), ("/api/restart", "POST"), ("/api/device", "GET"),
    ("/api/preview/snapshot", "GET"), ("/api/preview/stream", "GET"), ("/api/capture/devices", "GET"),
    ("/api/ndi/sources", "GET")])
def test_non_wall_routes_need_the_local_key_and_never_answer_cross_origin(path, method):
    assert D(path, method) == Decision(UNAUTHORIZED, False)
    assert D(path, method, local_key_ok=True) == Decision(ALLOW, False)
    assert D(path, method, origin=True) == Decision(UNAUTHORIZED, False)


def test_non_wall_route_from_the_lan_is_forbidden_even_with_the_key():
    assert D("/api/play", "POST", local=False, local_key_ok=True).verdict == FORBIDDEN


def test_non_wall_preflight_finds_nothing():
    assert D("/api/restart", "OPTIONS") == Decision(NOT_FOUND, False)


@pytest.mark.parametrize("with_key", [True, False])
def test_reboot_is_gone(with_key):
    assert D("/api/sys-reboot", "POST", local_key_ok=with_key) == Decision(NOT_FOUND, False)


@pytest.mark.parametrize("path,method", [
    ("/api/trigger", "POST"), ("/api/scene", "POST"), ("/api/alignment", "POST"),
    ("/api/interaction", "GET"), ("/api/patch", "POST")])
def test_a_sensor_on_the_lan_is_served(path, method):
    assert D(path, method, local=False) == Decision(ALLOW, True)
    assert D(path, method, local=False, wall_key_set=True, wall_key_ok=False) == Decision(UNAUTHORIZED, True)
    assert D(path, method, local=False, wall_key_set=True, wall_key_ok=True) == Decision(ALLOW, True)


def test_local_caller_without_origin_needs_no_trigger_key():
    assert D("/api/trigger", "POST", wall_key_set=True, wall_key_ok=False).verdict == ALLOW


def test_browser_on_this_machine_is_judged_as_a_lan_caller():
    assert D("/api/trigger", "POST", origin=True, wall_key_set=True, wall_key_ok=False).verdict == UNAUTHORIZED
    assert D("/api/trigger", "POST", origin=True, wall_key_set=True, wall_key_ok=True).verdict == ALLOW
    assert D("/api/trigger", "POST", origin=True).verdict == ALLOW


def test_browser_may_not_patch_unless_a_key_is_set():
    assert D("/api/patch", "POST", origin=True).verdict == FORBIDDEN
    assert D("/api/patch", "POST", origin=True, wall_key_set=True, wall_key_ok=True).verdict == ALLOW


def test_wall_preflight_is_answered():
    assert D("/api/trigger", "OPTIONS", local=False, origin=True) == Decision(ALLOW, True)


def test_wall_command_mapping():
    assert wall_command("/api/home", {}, {}) == ("set_scene", {"scene": "root", "stop": True})
    assert wall_command("/api/back", {}, {}) == ("set_scene", {"scene": "back", "stop": True})
    assert wall_command("/api/scene", {"scene": "x"}, {}) == ("set_scene", {"scene": "x"})
    assert wall_command("/api/play-content", {"content_id": "c"}, {}) == ("wall_play", {"content_id": "c"})
    assert wall_command("/api/trigger", {}, {"id": ["4"]}) == ("trigger", {"id": "4"})
    assert wall_command("/api/trigger", {"command": "explode"}, {}) == (None, "'explode' is not a trigger")


# ---- the server -----------------------------------------------------------------

@pytest.fixture
def api():
    calls = {"wall": [], "control": []}

    def handle_wall(cmd, args):
        calls["wall"].append((cmd, args))
        return True, f"{cmd} done", "home"

    srv = LanApiServer("LOCALKEY", lan_enabled=False, trigger_key="", handle_wall=handle_wall,
                       snapshot=lambda: {"interactive": True, "active_scene": "home"},
                       control=lambda name: calls["control"].append(name),
                       device_info=lambda: {"platform": "linux"}, capture_devices=lambda: [{"label": "Cam"}],
                       ndi_sources=lambda: {"available": False, "sources": []},
                       preview_jpeg=lambda: b"\xff\xd8jpeg", port=0)
    assert srv.start() is None
    srv.calls = calls
    yield srv
    srv.stop()


def req(srv, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    c.request(method, path, body=data, headers=headers or {})
    r = c.getresponse()
    raw = r.read()
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = raw
    return r.status, dict(r.getheaders()), payload


def test_trigger_from_a_sensor(api):
    status, h, body = req(api, "POST", "/api/trigger", {"command": "trigger", "id": 3})
    assert status == 200 and body == {"accepted": True, "reason": "trigger done", "scene": "home"}
    assert h["Access-Control-Allow-Origin"] == "*"
    req(api, "POST", "/api/trigger?id=7")
    assert api.calls["wall"][-1] == ("trigger", {"id": "7"})
    status, _, body = req(api, "POST", "/api/trigger", {"command": "explode"})
    assert status == 400 and body["reason"] == "'explode' is not a trigger"


def test_home_back_scene_play_alignment_patch(api):
    for path in ("/api/home", "/api/back", "/api/scene", "/api/play-content", "/api/alignment", "/api/patch"):
        assert req(api, "POST", path, {"scene": "s", "content_id": "c", "enabled": True, "element_id": "e"})[0] == 200
    assert [c for c, _ in api.calls["wall"]] == ["set_scene", "set_scene", "set_scene", "wall_play", "alignment", "patch"]


def test_bad_bodies(api):
    assert req(api, "POST", "/api/trigger", b"x" * 5000)[0] == 413
    status, _, body = req(api, "POST", "/api/trigger", b"[1,2]")
    assert status == 400 and body["reason"] == "body is not a JSON object"
    assert req(api, "GET", "/api/trigger")[0] == 405


def test_interaction_snapshot(api):
    status, h, body = req(api, "GET", "/api/interaction")
    assert status == 200 and body["active_scene"] == "home" and body["lan_triggers"] is False
    assert body["api_port"] == api.port


def test_preflight(api):
    status, h, _ = req(api, "OPTIONS", "/api/trigger", headers={"Origin": "http://x"})
    assert status == 204 and "X-Trigger-Key" in h["Access-Control-Allow-Headers"]


def test_local_routes_need_the_key_and_send_no_cors(api):
    status, h, _ = req(api, "POST", "/api/play")
    assert status == 401 and "Access-Control-Allow-Origin" not in h
    status, h, body = req(api, "POST", "/api/play", headers={"X-Local-Key": "LOCALKEY"})
    assert status == 200 and body == {"status": "success"} and api.calls["control"] == ["play"]
    assert "Access-Control-Allow-Origin" not in h
    assert req(api, "GET", "/api/device", headers={"X-Local-Key": "nope"})[0] == 401
    assert req(api, "GET", "/api/capture/devices", headers={"X-Local-Key": "LOCALKEY"})[2]["devices"] == [{"label": "Cam"}]
    status, h, body = req(api, "GET", "/api/preview/snapshot", headers={"X-Local-Key": "LOCALKEY"})
    assert status == 200 and h["Content-Type"] == "image/jpeg"
    assert req(api, "POST", "/api/sys-reboot", headers={"X-Local-Key": "LOCALKEY"})[0] == 404


def test_trigger_key_applies_to_browsers(api):
    api.trigger_key = "S3CRET"
    assert req(api, "POST", "/api/trigger", {"id": 1}, {"Origin": "http://page"})[0] == 401
    assert req(api, "POST", "/api/trigger", {"id": 1}, {"Origin": "http://page", "X-Trigger-Key": "S3CRET"})[0] == 200
    assert req(api, "POST", "/api/trigger", {"id": 1})[0] == 200           # a local sensor, no Origin
    api.trigger_key = ""
    assert req(api, "POST", "/api/patch", {"element_id": "e"}, {"Origin": "http://page"})[0] == 403

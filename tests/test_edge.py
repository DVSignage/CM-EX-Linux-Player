import asyncio

import httpx

from cmx.cms_client import CmsClient
from cmx.edge import CONTROL, MEDIA, TELEMETRY, EdgeRouter


def test_failover_after_two_failures_counted_per_class():
    events = []
    r = EdgeRouter("edge", "http://edge:8080", "https://cloud", lambda *a: events.append(a))
    assert r.base_for(CONTROL) == "http://edge:8080"
    assert not r.record(CONTROL, None)
    assert not r.record(MEDIA, 502)
    assert r.record(CONTROL, 500)                 # second control failure trips it
    assert r.base_for(CONTROL) == "https://cloud" and r.base_for(MEDIA) == "http://edge:8080"
    assert events[0][:2] == ("edge_failover", CONTROL)
    assert r.base_for(TELEMETRY) == "http://edge:8080"   # telemetry never fails over


def test_backpressure_and_answers_are_not_failures():
    r = EdgeRouter("edge", "http://edge", "https://cloud")
    r.record(CONTROL, None)
    r.record(CONTROL, 404)                         # an answer resets the count
    assert not r.record(CONTROL, None)
    assert not r.record(CONTROL, 503, retry_after=5)
    assert not r.failed[CONTROL]


def test_probe_fails_back_but_respects_forced_hold():
    t = [0.0]
    r = EdgeRouter("edge", "http://edge", "https://cloud", clock=lambda: t[0])
    r.record(MEDIA, None)
    r.record(MEDIA, None)
    r.force_failover(CONTROL, 120, "stale replica")
    r.probe_result(True)
    assert not r.failed[MEDIA] and r.failed[CONTROL]
    t[0] = 121
    r.probe_result(True)
    assert not r.failed[CONTROL]


def test_cloud_mode_router_is_inert():
    r = EdgeRouter("cloud", "http://edge", "https://cloud")
    assert not r.enabled and r.base_for(CONTROL) == "https://cloud" and not r.record(CONTROL, None)


def test_client_routes_heartbeats_and_retries_on_cloud_when_tripped():
    hosts = []

    def handler(req):
        hosts.append(req.url.host)
        if req.url.host == "edge":
            return httpx.Response(502)
        return httpx.Response(200, json={"command": "none"})

    router = EdgeRouter("edge", "http://edge:8080", "https://cloud")
    c = CmsClient("https://cloud", lambda: "s", httpx.MockTransport(handler), router)

    async def go():
        r1 = await c.heartbeat("P", {})
        assert r1.status_code == 502 and c.last_from_edge
        r2 = await c.heartbeat("P", {})          # second failure: retried on the cloud
        assert r2.status_code == 200 and not c.last_from_edge
        r3 = await c.heartbeat("P", {})
        assert r3.status_code == 200
    asyncio.run(go())
    assert hosts == ["edge", "edge", "cloud", "cloud"]


def test_edge_telemetry_falls_back_to_events_path():
    paths = []

    def handler(req):
        paths.append(req.url.path)
        if req.url.path.endswith("/telemetry"):
            return httpx.Response(404)
        return httpx.Response(200, json={"accepted": 1})

    router = EdgeRouter("edge", "http://edge", "https://cloud")
    c = CmsClient("https://cloud", lambda: None, httpx.MockTransport(handler), router)

    async def go():
        await c.telemetry("P", [{"event_id": "e"}])
        await c.telemetry("P", [{"event_id": "f"}])
    asyncio.run(go())
    assert paths == ["/api/v1/players/P/telemetry", "/api/v1/players/P/events", "/api/v1/players/P/events"]


def test_content_meta_asks_edge_first_then_cloud():
    hosts = []

    def handler(req):
        hosts.append(req.url.host)
        if req.url.host == "edge":
            return httpx.Response(404)
        return httpx.Response(200, json={"filename": "a.mp4"})

    c = CmsClient("https://cloud", lambda: None, httpx.MockTransport(handler),
                  EdgeRouter("edge", "http://edge", "https://cloud"))
    assert asyncio.run(c.content_meta("c1")) == {"filename": "a.mp4"}
    assert hosts == ["edge", "cloud"]

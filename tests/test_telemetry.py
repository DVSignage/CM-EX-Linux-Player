import asyncio
import json
from datetime import datetime, timezone

import httpx

from cmx.telemetry import (TelemetryQueue, boot_event, crash_event, edge_event, pending_crash_markers,
                           scrub, ts, write_crash_marker)


def ev(i, type_="edge_event"):
    return {"event_id": f"e{i}", "type": type_, "code": f"c{i}"}


def test_ts_format():
    assert ts(datetime(2026, 11, 23, 9, 5, 7, 123456, tzinfo=timezone.utc)) == "2026-11-23T09:05:07.123Z"


def test_ids_are_stable_and_unique(tmp_path):
    q = TelemetryQueue(tmp_path / "q.json")
    assert q.enqueue(ev(1))
    assert not q.enqueue(ev(1))
    e = edge_event("p", "info", "boot", "m", {})
    assert e["event_id"]
    q.enqueue(e)
    assert json.loads((tmp_path / "q.json").read_text())[1]["event_id"] == e["event_id"]
    assert len(TelemetryQueue(tmp_path / "q.json").events) == 2     # survives a restart


def test_cap_evicts_oldest_non_proof_of_play(tmp_path):
    q = TelemetryQueue(tmp_path / "q.json", cap_=3)
    q.enqueue(ev(0, "proof_of_play"))
    for i in range(1, 4):
        q.enqueue(ev(i))
    assert [e["event_id"] for e in q.events] == ["e0", "e2", "e3"]


def _flush(q, responder):
    sent = []

    async def send(batch):
        sent.append([e["event_id"] for e in batch])
        return responder(batch)

    left = asyncio.run(q.flush(send))
    return left, sent


def test_flush_removes_accepted_and_keeps_on_5xx(tmp_path):
    q = TelemetryQueue(tmp_path / "q.json")
    for i in range(3):
        q.enqueue(ev(i))
    left, _ = _flush(q, lambda b: httpx.Response(503))
    assert left == 0 and len(q.events) == 3
    left, sent = _flush(q, lambda b: httpx.Response(200, json={"accepted": len(b), "duplicates": 0}))
    assert left == 3 and q.events == [] and sent == [["e0", "e1", "e2"]]


def test_schema_refusal_isolates_and_drops_only_the_bad_event(tmp_path):
    q = TelemetryQueue(tmp_path / "q.json")
    for i in range(4):
        q.enqueue(ev(i))

    def responder(batch):
        if any(e["event_id"] == "e2" for e in batch):
            return httpx.Response(422)
        return httpx.Response(200, json={"accepted": len(batch)})

    _flush(q, responder)
    assert q.events == []           # e0, e1, e3 delivered, e2 dropped alone


def test_only_cloud_types_are_sent(tmp_path):
    q = TelemetryQueue(tmp_path / "q.json")
    q.enqueue(ev(1, "proof_of_play"))
    q.enqueue(ev(2))
    _, sent = _flush(q, lambda b: httpx.Response(200))
    assert sent == [["e2"]]
    assert [e["event_id"] for e in q.events] == ["e1"]      # held, not dropped


def test_scrub():
    text = ("GET https://user:pw@cms.example.com/api?token=abc#x failed for bob@example.com "
            "password=hunter2 Authorization: Bearer eyJabc.def cmx_ABCDEFGH1234 /home/alice/app")
    out = scrub(text)
    assert "https://cms.example.com/api" in out and "token=abc" not in out and "user:pw" not in out
    assert "bob@example.com" not in out and "hunter2" not in out and "eyJabc" not in out
    assert "cmx_ABCDEFGH1234" not in out and "/home/<user>/app" in out


def test_boot_and_crash_events(tmp_path):
    b = boot_event("P1", "2.0.0", datetime(2026, 1, 1, tzinfo=timezone.utc), True)
    assert b["type"] == "edge_event" and b["code"] == "boot" and b["detail"] == {"version": "2.0.0", "after_crash": True}
    try:
        raise ValueError("bad thing at https://h/x?secret=1")
    except ValueError as e:
        path = write_crash_marker(tmp_path, "2.0.0", e)
    marker = json.loads(path.read_text())
    c = crash_event("P1", marker)
    assert c["event_id"] == marker["event_id"] and c["code"] == "crash" and c["level"] == "error"
    assert c["message"].endswith("crashed: ValueError")
    assert "secret=1" not in c["detail"]
    assert pending_crash_markers(tmp_path) == [path]

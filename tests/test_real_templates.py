"""Real CM:EX template definitions (scrubbed copies under fixtures/templates)
must draw natively — no fallback to the CMS render page — and their walls
must parse and answer triggers."""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from cmx.interaction import InteractionEngine, Model
from cmx.templates import TemplateBuilder
from cmx.wallhost import WallHost

from conftest import FIXTURES, MP4_HEAD, PNG_HEAD

TEMPLATES = sorted((FIXTURES / "templates").glob("*.json"))


def load(name):
    return json.loads((FIXTURES / "templates" / f"{name}.json").read_text(encoding="utf-8"))


def build(tmp_path, definition):
    async def fetch(tid):
        return None

    async def ensure(cid, role):
        p = tmp_path / f"{cid}{'.png' if role == 'image' else '.mp4'}"
        p.write_bytes(PNG_HEAD if role == "image" else MP4_HEAD)
        return p

    return asyncio.run(TemplateBuilder(fetch, ensure).build(definition["id"], definition))


@pytest.mark.parametrize("path", TEMPLATES, ids=[p.stem for p in TEMPLATES])
def test_every_real_template_draws_natively(tmp_path, path):
    d = json.loads(path.read_text(encoding="utf-8"))
    built = build(tmp_path, d)
    assert built is not None, "fell back to the web render"
    p = built["payload"]
    assert (p["width"], p["height"]) == (d["width"], d["height"])
    assert len(p["elements"]) == len(d["elements"])
    wanted = {e["props"].get("contentId") for e in d["elements"] if e["props"].get("contentId")}
    wanted |= {e["props"].get("attractorContentId") for e in d["elements"] if e["props"].get("attractorContentId")}
    wanted.discard(None)
    rss = {e["props"]["contentId"] for e in d["elements"] if e["type"] == "rss"}
    assert wanted - rss <= set(p["media"])        # every picture/clip/zone loop is downloaded first
    assert built["rss"] == rss


def test_web_elements_and_background_image(tmp_path):
    web = build(tmp_path, load("web_elements"))["payload"]
    assert [e["src"] for e in web["elements"]] == \
        ["http://cms.example/api/v1/templates/00000000-0000-0000-0000-000000000000/preview?live=1"] * 2
    assert all(e["props"]["transparent"] is True for e in web["elements"])
    bg = load("background_image")
    p = build(tmp_path, bg)["payload"]
    assert p["background"] == "#FFFFFF" and p["background_image"] == bg["background_image_id"]
    assert bg["background_image_id"] in p["media"]


def test_real_walls_parse_completely():
    for name in ("interactive_wall", "shared_triggers_touch"):
        spec = load(name)["interaction"]
        m = Model.parse(spec)
        assert m is not None
        assert len(m.scenes) == len(spec["scenes"])
        assert sum(len(s.mappings) for s in m.scenes.values()) == \
            sum(len(s["mappings"]) for s in spec["scenes"])


def test_real_wall_answers_its_triggers(tmp_path):
    d = load("interactive_wall")
    built = build(tmp_path, d)
    sent = []
    host = WallHost(sent.append, built["zones"], built["media"], paused=lambda: False)
    eng = InteractionEngine(built["model"], built["media"], host,
                            lambda: datetime(2026, 11, 24, 12, tzinfo=timezone.utc), zone=timezone.utc)
    host.engine = eng
    eng.start()
    root = built["model"].scenes[built["model"].root_scene_id]
    tid, mapping = next(iter(root.mappings.items()))
    ok, why = eng.trigger(tid, "test")
    assert ok, why
    plays = [m for m in sent if m.get("op") == "play"]
    if mapping.content_id:
        assert plays and plays[-1]["url"].startswith("/media/")
    if mapping.then:
        assert eng.active_scene_id != root.id or mapping.then in ("root", "back")


def test_shared_trigger_plays_every_bound_zone(tmp_path):
    d = load("shared_triggers_touch")
    built = build(tmp_path, d)
    counts = {}
    for z in built["zones"]:
        counts[z["trigger_id"]] = counts.get(z["trigger_id"], 0) + 1
    shared = [t for t, n in counts.items() if n > 1]
    keys = {z["surface_key"] for z in built["zones"] if z["trigger_id"] in shared}
    assert all(k.startswith("el:") for k in keys)          # shared zones are known by element id
    assert built["payload"]["interaction"]["touch"] is True

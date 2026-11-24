import asyncio

from cmx.templates import (TemplateBuilder, capture_audio, device_label, media_id, nested_placement,
                           patch_definition, zone_trigger_id)

from conftest import MP4_HEAD, PNG_HEAD

DEF = {
    "width": 1920, "height": 1080, "background_color": "#123",
    "elements": [
        {"id": "t1", "type": "text", "x": 0, "y": 0, "width": 100, "height": 50, "props": {"content": "Hi"}},
        {"id": "i1", "type": "image", "x": 0, "y": 0, "width": 100, "height": 50, "props": {"mediaId": "img"}},
        {"id": "v1", "type": "video", "x": 0, "y": 0, "width": 100, "height": 50, "props": {"content_id": "vid", "triggerId": 7}},
        {"id": "z1", "type": "trigger_zone", "x": 0, "y": 0, "width": 10, "height": 10, "props": {"triggerId": 1}},
        {"id": "z2", "type": "trigger_zone", "x": 0, "y": 0, "width": 10, "height": 10, "props": {"triggerId": 2}},
        {"id": "z3", "type": "trigger_zone", "x": 0, "y": 0, "width": 10, "height": 10, "props": {"triggerId": 2}},
        {"id": "r1", "type": "rss", "x": 0, "y": 0, "width": 10, "height": 10, "props": {"contentId": "feed"}},
        {"id": "n1", "type": "template", "x": 0, "y": 0, "width": 960, "height": 540, "props": {"templateId": "child"}},
    ],
    "interaction": {"rootSceneId": "s", "scenes": [{"id": "s", "mappings": [{"triggerId": 1, "contentId": "wallclip"}]}]},
}


def builder(tmp_path, defs):
    fetched = []

    async def fetch(tid):
        fetched.append(tid)
        return defs.get(tid)

    async def ensure(cid, role):
        p = tmp_path / f"{cid}{'.png' if role == 'image' else '.mp4'}"
        p.write_bytes(PNG_HEAD if role == "image" else MP4_HEAD)
        return p

    return TemplateBuilder(fetch, ensure), fetched


def test_build_resolves_media_nested_rss_zones_and_wall(tmp_path):
    child = {"width": 100, "height": 100, "elements": [{"id": "c", "type": "image", "props": {"contentId": "cimg"}}]}
    b, fetched = builder(tmp_path, {"child": child})
    built = asyncio.run(b.build("T", DEF))
    p = built["payload"]
    assert set(p["media"]) == {"img", "vid", "cimg", "wallclip"}
    assert p["media"]["img"] == {"url": "/media/img.png", "kind": "image"}
    assert built["rss"] == {"feed"}
    assert "template_child.json" in built["files"]
    n = next(e for e in p["elements"] if e["id"] == "n1")["nested"]
    assert n["fit"] == "contain" and n["elements"][0]["media"] == "cimg"
    keys = {e["id"]: e["surface_key"] for e in p["elements"] if e["type"] == "trigger_zone"}
    assert keys == {"z1": "zone:1", "z2": "el:z2", "z3": "el:z3"}   # shared trigger 2: by element id
    assert next(e for e in p["elements"] if e["id"] == "v1")["tap_trigger"] == 7
    assert built["model"] is not None and p["interaction"]["has_fullscreen"] is False


def test_unknown_type_falls_back_and_nested_cycles_are_cut(tmp_path):
    b, _ = builder(tmp_path, {})
    assert asyncio.run(b.build("T", {"elements": [{"id": "x", "type": "chart"}]})) is None
    loop = {"elements": [{"id": "n", "type": "template", "props": {"templateId": "A"}}]}
    b, fetched = builder(tmp_path, {"A": loop})
    built = asyncio.run(b.build("A", loop))
    assert built["payload"]["elements"][0]["nested"] is None and fetched == []


def test_nested_depth_is_capped(tmp_path):
    chain = {f"T{i}": {"elements": [{"id": "n", "type": "template", "props": {"templateId": f"T{i + 1}"}}]}
             for i in range(6)}
    b, fetched = builder(tmp_path, chain)
    asyncio.run(b.build("T0", chain["T0"]))
    assert fetched == ["T1", "T2", "T3"]


def test_prop_readers():
    assert media_id({"asset_id": 5}) == "5"
    assert device_label({"capture_device_label": " HDMI "}) == "HDMI"
    assert capture_audio({}) is True and capture_audio({"muted": True}) is False
    assert capture_audio({"audioEnabled": False}) is False
    assert zone_trigger_id({"triggerId": "12"}) == 12 and zone_trigger_id({"triggerId": 0}) is None
    assert zone_trigger_id({"triggerId": 3.5}) is None


def test_nested_placement():
    assert nested_placement("contain", 200, 100, 100, 100) == (1.0, 1.0, 50.0, 0.0)
    assert nested_placement("cover", 200, 100, 100, 100) == (2.0, 2.0, 0.0, -50.0)
    assert nested_placement("stretch", 200, 100, 100, 100) == (2.0, 1.0, 0.0, 0.0)


def test_patch_definition_messages():
    assert patch_definition(DEF, {}) == (False, "template_patch needs 'element_id'", None)
    assert patch_definition(DEF, {"element_id": "zz", "kind": "text"})[1] == "element zz not found"
    assert patch_definition(DEF, {"element_id": "z1", "kind": "trigger_zone"})[1] == "kind trigger_zone not patchable"
    assert patch_definition(DEF, {"element_id": "i1", "kind": "image"})[1] == "template_patch needs 'content_id'"
    ok, msg, d = patch_definition(DEF, {"element_id": "i1", "kind": "image", "content_id": "new"})
    assert ok and msg == "patched i1"
    assert d["elements"][1]["props"] == {"contentId": "new"}
    assert DEF["elements"][1]["props"] == {"mediaId": "img"}       # the original is untouched
    ok, msg, d = patch_definition(DEF, {"element_id": "t1", "kind": "text", "text": "Bye"})
    assert d["elements"][0]["props"]["content"] == "Bye"

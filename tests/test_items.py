from pathlib import Path

from cmx.cms_client import CmsClient
from cmx.items import (IMAGE, TEMPLATE, VIDEO, WEB, Item, cache_name, classify, clip_bound,
                       media_dwell, same_content)

CMS = CmsClient("http://cms:8000", lambda: None)
CACHE = Path("/cache")


def item(**kw):
    base = {"content_id": "c1", "duration": 0}
    base.update(kw)
    return base


def test_template_uses_template_render_url_from_content_url():
    it = classify(item(content={"type": "template", "url": "http://other/api/v1/templates/T9/render",
                                "name": "Menu"}), CMS, CACHE)
    assert it.kind == TEMPLATE
    assert it.template_id == "T9"
    assert it.url == "http://cms:8000/api/v1/templates/T9/render"   # our base, not content.url's host


def test_rss_never_uses_publisher_url():
    it = classify(item(content={"type": "RSS", "url": "https://news.example/feed.xml"}), CMS, CACHE)
    assert it.kind == WEB
    assert it.url == "http://cms:8000/api/v1/rss/c1/render"


def test_web_aliases_use_content_url_or_stream():
    for t in ("url", "webpage", "iframe", "link", "embed", "web"):
        it = classify(item(content={"type": t, "url": " https://example.com/page "}), CMS, CACHE)
        assert (it.kind, it.url) == (WEB, "https://example.com/page")
    it = classify(item(content={"type": "url"}), CMS, CACHE)
    assert it.url == "http://cms:8000/api/v1/content/c1/stream"


def test_media_is_cached_by_content_id_not_filename():
    a = classify(item(content_id="a", content={"filename": "promo.mp4"}), CMS, CACHE)
    b = classify(item(content_id="b", content={"filename": "promo.mp4"}), CMS, CACHE)
    assert a.path != b.path
    assert a.path.name == "a.mp4" and b.path.name == "b.mp4"
    assert cache_name("x", "") == "x.mp4"


def test_image_classification_and_dwell():
    it = classify(item(content={"filename": "pic.JPG"}, duration=0), CMS, CACHE)
    assert it.kind == IMAGE
    assert it.dwell(10) == 10
    assert Item(kind=IMAGE, content_id="x", name="x", duration=7).dwell(10) == 7
    assert Item(kind=WEB, content_id="x", name="x").dwell(10) == 30
    assert Item(kind=VIDEO, content_id="x", name="x", duration=12).dwell(10) == 0   # mpv enforces


def test_media_dwell_only_cuts_meaningfully_short_durations():
    assert media_dwell(83, 82.96) == 0      # CM:EX auto-filled from the clip length
    assert media_dwell(0, 60) == 0
    assert media_dwell(20, 60) == 20         # operator cut-off
    assert media_dwell(20, None) == 20


def test_clip_bounds_tolerate_strings_and_drop_bad_values():
    assert clip_bound({"clip_start": "12.5"}, "clip_start") == 12.5
    assert clip_bound({"clip_start": -3}, "clip_start") is None
    assert clip_bound({"clip_start": "abc"}, "clip_start") is None
    it = classify(item(content={"filename": "v.mp4"}, clip_start=30, clip_end=10), CMS, CACHE)
    assert it.clip_start == 30 and it.clip_end is None          # inverted window ignored


def test_out_point_caps_clip_end_with_dwell():
    it = classify(item(content={"filename": "v.mp4", "duration": 120}, clip_start=10, clip_end=50,
                       duration=20), CMS, CACHE)
    assert it.in_point == 10
    assert it.out_point == 30       # min(50, 10 + 20)


def test_same_content_includes_raw_clip_bounds():
    a = [classify(item(content={"filename": "v.mp4"}, clip_start=10, clip_end=20), CMS, CACHE)]
    b = [classify(item(content={"filename": "v.mp4"}, clip_start=30, clip_end=40), CMS, CACHE)]
    c = [classify(item(content={"filename": "v.mp4"}, clip_start=10, clip_end=20), CMS, CACHE)]
    assert not same_content(a, b)
    assert same_content(a, c)
    assert not same_content([], [])


def test_item_without_content_id_is_skipped():
    assert classify({"content": {"filename": "x.mp4"}}, CMS, CACHE) is None

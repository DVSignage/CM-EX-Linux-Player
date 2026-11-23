import asyncio
import json

import httpx
import pytest

from cmx.cache import (BACKOFF, CacheStore, PendingDownloads, identify, is_image_file, misnamed_as,
                       repair_name)
from cmx.cms_client import CmsClient, CmsError, ContentGone, ContentNotReady

from conftest import MP4_HEAD, PNG_HEAD


def test_identify_signatures():
    assert identify(PNG_HEAD) == ("image", ".png")
    assert identify(b"\xff\xd8\xff\xe0" + b"\x00" * 12) == ("image", ".jpg")
    assert identify(MP4_HEAD) == ("video", ".mp4")
    assert identify(b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 4) == ("video", ".mov")
    assert identify(b"\x1a\x45\xdf\xa3" + b"\x00" * 12) == ("video", ".webm")
    assert identify(b"hello world12345") == ("unknown", None)


def test_bytes_decide_picture_vs_video(tmp_path):
    p = tmp_path / "c1.mp4"
    p.write_bytes(PNG_HEAD)
    assert is_image_file(p)
    assert misnamed_as(p) == ".png"
    fixed = repair_name(p)
    assert fixed.name == "c1.png" and fixed.exists() and not p.exists()


def test_find_repairs_a_misnamed_cache_file(tmp_path):
    store = CacheStore(tmp_path / "cache", tmp_path / "h.json")
    store.dir.mkdir()
    (store.dir / "c9.mp4").write_bytes(PNG_HEAD)
    found = store.find("c9", store.dir / "c9.jpg")
    assert found == store.dir / "c9.png"
    assert store.find("missing", store.dir / "missing.mp4") is None


def test_retention_keeps_the_union_of_recent_playlists(tmp_path):
    store = CacheStore(tmp_path / "cache", tmp_path / "h.json", keep_playlists=2)
    store.dir.mkdir()
    for n in ("a.mp4", "b.mp4", "c.mp4", "x.mp4.tmp"):
        (store.dir / n).write_bytes(b"x")
    store.retain_and_prune({"a.mp4"})
    assert sorted(p.name for p in store.dir.iterdir()) == ["a.mp4", "x.mp4.tmp"]   # .tmp never pruned
    (store.dir / "b.mp4").write_bytes(b"x")
    store.retain_and_prune({"b.mp4"})
    assert sorted(p.name for p in store.dir.iterdir()) == ["a.mp4", "b.mp4", "x.mp4.tmp"]
    store.retain_and_prune({"b.mp4"})          # same set again: deduped, a is still kept
    assert (store.dir / "a.mp4").exists()
    (store.dir / "c.mp4").write_bytes(b"x")
    store.retain_and_prune({"c.mp4"})          # N=2: a falls out
    assert sorted(p.name for p in store.dir.iterdir()) == ["b.mp4", "c.mp4", "x.mp4.tmp"]
    assert json.loads((tmp_path / "h.json").read_text()) == [["c.mp4"], ["b.mp4"]]


def test_lru_cap_for_direct_picks(tmp_path):
    store = CacheStore(tmp_path / "cache", tmp_path / "h.json")
    store.dir.mkdir()
    import os
    for i in range(5):
        p = store.dir / f"f{i}.mp4"
        p.write_bytes(b"x")
        os.utime(p, (1000 + i, 1000 + i))
    removed = store.enforce_lru({"f0.mp4"}, max_unassigned=2)
    assert removed == 2
    assert sorted(p.name for p in store.dir.iterdir()) == ["f0.mp4", "f3.mp4", "f4.mp4"]


def test_pending_backoff_and_fault():
    now = [0.0]
    p = PendingDownloads(clock=lambda: now[0])
    from pathlib import Path
    for _ in range(3):
        p.failed("c1", Path("c1.mp4"), "promo.mp4")
    assert p.entries["c1"].attempts == 3
    assert p.entries["c1"].retry_at == BACKOFF[2]
    assert p.fault() == "promo.mp4 could not be downloaded after 3 attempts; still retrying"
    p.not_ready("c2", Path("c2.mp4"), "b.mp4", 45)
    assert p.entries["c2"].attempts == 0 and p.entries["c2"].retry_at == 45
    p.keep_only({"c2"})
    assert list(p.entries) == ["c2"]


def _client(handler):
    return CmsClient("http://cms", lambda: "s3cret", httpx.MockTransport(handler))


def test_download_resumes_with_range(tmp_path):
    dest = tmp_path / "c1.mp4"
    (tmp_path / "c1.mp4.tmp").write_bytes(b"0123")
    seen = {}

    def handler(request):
        seen["range"] = request.headers.get("Range")
        seen["secret"] = request.headers.get("X-Player-Secret")
        return httpx.Response(206, content=b"4567", headers={"Content-Range": "bytes 4-7/8"})

    asyncio.run(_client(handler).download("c1", dest))
    assert seen == {"range": "bytes=4-", "secret": "s3cret"}
    assert dest.read_bytes() == b"01234567"
    assert not (tmp_path / "c1.mp4.tmp").exists()


def test_download_200_restarts_partial(tmp_path):
    dest = tmp_path / "c1.mp4"
    (tmp_path / "c1.mp4.tmp").write_bytes(b"junk")
    asyncio.run(_client(lambda r: httpx.Response(200, content=b"fresh")).download("c1", dest))
    assert dest.read_bytes() == b"fresh"


def test_download_status_handling(tmp_path):
    dest = tmp_path / "c1.mp4"
    with pytest.raises(ContentNotReady) as e:
        asyncio.run(_client(lambda r: httpx.Response(503, headers={"Retry-After": "20"})).download("c1", dest))
    assert e.value.retry_after == 20
    with pytest.raises(ContentGone):
        asyncio.run(_client(lambda r: httpx.Response(410)).download("c1", dest))
    (tmp_path / "c1.mp4.tmp").write_bytes(b"stale")
    with pytest.raises(CmsError):
        asyncio.run(_client(lambda r: httpx.Response(416)).download("c1", dest))
    assert not (tmp_path / "c1.mp4.tmp").exists()

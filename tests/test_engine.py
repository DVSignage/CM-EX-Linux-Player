import asyncio

from cmx.items import IMAGE, TEMPLATE, VIDEO, Item
from cmx.playback import PAUSED, PLAYING, STOPPED, WAITING, PlaylistEngine
from cmx.wall import crop_filter_normalised

from conftest import MP4_HEAD, PNG_HEAD


def make_engine(mpv, browser, tmp_path):
    events = {"waiting": 0, "blank": 0}

    def on_waiting():
        events["waiting"] += 1

    def blank():
        events["blank"] += 1

    def resolve(item):
        return item.path if item.path and item.path.exists() else None

    eng = PlaylistEngine(mpv, browser, default_image=10, resolve=resolve, blank=blank,
                         on_waiting=on_waiting, crop_filter=crop_filter_normalised)
    return eng, events


def video(tmp_path, cid, exists=True, **kw):
    p = tmp_path / f"{cid}.mp4"
    if exists:
        p.write_bytes(MP4_HEAD)
    return Item(kind=VIDEO, content_id=cid, name=f"{cid}.mp4", path=p, **kw)


def image(tmp_path, cid):
    p = tmp_path / f"{cid}.png"
    p.write_bytes(PNG_HEAD)
    return Item(kind=IMAGE, content_id=cid, name=f"{cid}.png", path=p)


def test_skips_items_not_downloaded_and_waits_when_none_ready(fake_mpv, fake_browser, tmp_path):
    async def go():
        eng, ev = make_engine(fake_mpv, fake_browser, tmp_path)
        await eng.load([video(tmp_path, "a", exists=False), video(tmp_path, "b")])
        assert eng.index == 1 and fake_mpv.loads[-1][0].endswith("b.mp4")
        await eng.load([video(tmp_path, "x", exists=False)])
        assert eng.mode == WAITING and ev["waiting"] == 1
    asyncio.run(go())


def test_identical_reapply_keeps_position(fake_mpv, fake_browser, tmp_path):
    async def go():
        eng, _ = make_engine(fake_mpv, fake_browser, tmp_path)
        items = [video(tmp_path, "a"), video(tmp_path, "b")]
        await eng.load(items)
        await eng.advance()
        loads = len(fake_mpv.loads)
        replaced = await eng.load([video(tmp_path, "a"), video(tmp_path, "b")])
        assert not replaced and eng.index == 1 and len(fake_mpv.loads) == loads
    asyncio.run(go())


def test_single_video_loops_in_place_with_clip(fake_mpv, fake_browser, tmp_path):
    async def go():
        eng, _ = make_engine(fake_mpv, fake_browser, tmp_path)
        await eng.load([video(tmp_path, "a", clip_start=5, clip_end=15)])
        path, kw = fake_mpv.loads[-1]
        assert kw["loop"] is True and kw["start"] == 5 and kw["end"] == 15
        eng.on_eof(fake_mpv.generation)
        await eng.advance()
        assert len(fake_mpv.loads) == 1      # never rebuilt
    asyncio.run(go())


def test_eof_advances_only_for_the_current_generation(fake_mpv, fake_browser, tmp_path):
    async def go():
        eng, _ = make_engine(fake_mpv, fake_browser, tmp_path)
        await eng.load([video(tmp_path, "a"), video(tmp_path, "b")])
        eng.on_eof(fake_mpv.generation - 5)    # stale
        await asyncio.sleep(0)
        assert eng.index == 0
        eng.on_eof(fake_mpv.generation)
        await asyncio.sleep(0.01)
        assert eng.index == 1
    asyncio.run(go())


def test_image_dwell_and_pause_freezes_timer(fake_mpv, fake_browser, tmp_path):
    async def go():
        eng, _ = make_engine(fake_mpv, fake_browser, tmp_path)
        a = image(tmp_path, "a")
        a.duration = 0.05
        await eng.load([a, video(tmp_path, "b")])
        eng.pause()
        assert eng.mode == PAUSED
        await asyncio.sleep(0.1)
        assert eng.index == 0                  # frozen while paused
        await eng.play()
        await asyncio.sleep(0.15)
        assert eng.index == 1
    asyncio.run(go())


def test_stop_blanks_until_play(fake_mpv, fake_browser, tmp_path):
    async def go():
        eng, ev = make_engine(fake_mpv, fake_browser, tmp_path)
        await eng.load([video(tmp_path, "a"), video(tmp_path, "b")])
        await eng.stop()
        assert eng.mode == STOPPED and ev["blank"] == 1
        assert eng.status()["playback_status"] == "stopped" and eng.status()["position"] is None
        await eng.play()
        assert eng.mode == PLAYING
    asyncio.run(go())


def test_seek_is_clip_relative_and_refuses_images(fake_mpv, fake_browser, tmp_path):
    async def go():
        eng, _ = make_engine(fake_mpv, fake_browser, tmp_path)
        await eng.load([video(tmp_path, "a", clip_start=10, clip_end=40), image(tmp_path, "b")])
        fake_mpv.time_pos, fake_mpv.duration = 12.0, 120.0
        assert eng.seek(5, None) == (True, "seeked to 5s of 30s")
        assert fake_mpv.seeks[-1] == 15
        assert eng.seek(None, 100) == (True, "seeked to 30s of 30s")
        st = eng.status()
        assert st["position"] == 2.0 and st["duration"] == 30.0
        await eng.advance()
        assert eng.seek(1, None) == (False, "not seekable")
        assert eng.seek(None, None) == (False, "seek needs 'seconds' or 'position'")
    asyncio.run(go())


def test_template_scaled_to_screen_and_single_page_not_relaunched(fake_mpv, fake_browser, tmp_path):
    async def go():
        eng, _ = make_engine(fake_mpv, fake_browser, tmp_path)
        t = Item(kind=TEMPLATE, content_id="t", name="Menu", url="http://cms/api/v1/templates/T/render",
                 canvas=(3840, 2160))
        await eng.load([t])
        assert fake_browser.shown == [("http://cms/api/v1/templates/T/render", 0.5)]
        await eng.advance()
        assert len(fake_browser.shown) == 1
        ok, _ = await eng.replay()
        assert ok and len(fake_browser.shown) == 2
    asyncio.run(go())


def test_crop_applies_to_media(fake_mpv, fake_browser, tmp_path):
    async def go():
        eng, _ = make_engine(fake_mpv, fake_browser, tmp_path)
        await eng.load([video(tmp_path, "a")])
        eng.set_crop((0.5, 0.0, 0.5, 1.0))
        assert fake_mpv.filter == "crop=w=iw*0.5:h=ih*1:x=iw*0.5:y=ih*0,setsar=1"
        eng.set_crop(None)
        assert fake_mpv.filter is None
    asyncio.run(go())

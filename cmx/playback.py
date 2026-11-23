"""
Playback: the mpv wrapper and the playlist engine.

Semantics follow the DVSI Plus window (`CmxPlayerWindow`):
  - image / web / template items advance on a dwell timer; video plays to
    its out-point (clip end, or an operator cut-off), enforced by mpv;
  - a single-item playlist never advances and is never rebuilt — a video
    loops in place, a page keeps running;
  - items whose file has not landed yet are skipped; with nothing ready the
    "waiting for content" screen shows;
  - `stop` blanks the screen until play / new content; `pause` holds the
    picture and freezes the dwell timer;
  - an identical re-apply keeps the playback position (`same_content`).
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable, List, Optional

from . import cache as cachemod
from .items import IMAGE, TEMPLATE, VIDEO, Item, same_content

try:
    import mpv as _mpv_module
except (ImportError, OSError):   # OSError: libmpv missing
    _mpv_module = None

log = logging.getLogger("player")


# ---------------------------------------------------------------------------
# mpv wrapper
# ---------------------------------------------------------------------------

class MpvPlayer:
    """Thin wrapper around python-mpv. mpv callbacks fire on mpv's own thread;
    they are handed to the asyncio loop with call_soon_threadsafe()."""

    def __init__(self, display: str = ":0", audio_output: str = "auto"):
        if _mpv_module is None:
            raise RuntimeError("python-mpv / libmpv is not installed.  Run: pip install python-mpv")
        self.display = display
        self.audio_output = audio_output
        self._mpv = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._eof_callback: Optional[Callable[[int], None]] = None
        self.generation = 0          # bumped on every load; EOF reports carry it
        self._eof_reported = -1

    def initialise(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        kwargs = dict(
            input_default_bindings=False,
            input_vo_keyboard=False,
            osc=False,
            ytdl=False,
            idle=True,                  # keep the window with nothing playing
            keep_open="always",         # hold the last frame at EOF (smooth cuts)
            force_window="yes",
            hwdec="auto",
            fs=True,
            really_quiet=True,
            osd_level=0,
            osd_font_size=48,
            osd_duration=10000,
            stop_screensaver="yes",
            prefetch_playlist="yes",
            demuxer_readahead_secs=5,
            image_display_duration="inf",
        )
        if self.audio_output and self.audio_output != "auto":
            kwargs["audio_device"] = self.audio_output

        # mpv opens the video output lazily, on the first file, so creating
        # MPV(vo="gpu") succeeds even where GL is unusable and the screen then
        # stays black. Hand mpv the whole list instead: it falls back itself
        # (gpu → x11 → drm) at the moment the output is opened.
        for vo in ["gpu,x11,drm", ""]:
            try:
                if vo:
                    kwargs["vo"] = vo
                self._mpv = _mpv_module.MPV(**kwargs)
                log.info(f"mpv initialised (vo={vo or 'auto'}, fullscreen)")
                break
            except Exception as e:
                log.warning(f"mpv vo={vo or 'auto'} failed: {e}")
                kwargs.pop("vo", None)
        else:
            raise RuntimeError("Could not initialise mpv with any video output driver")

        # With keep-open, mpv pauses at the end instead of unloading, so the end
        # is seen as eof-reached; end-file(eof) covers files mpv does unload.
        @self._mpv.property_observer("eof-reached")
        def _on_eof_reached(_name, value):
            if value:
                self._report_eof()

        @self._mpv.event_callback("end-file")
        def _on_end_file(event):
            reason = None
            try:
                if isinstance(event, dict):
                    reason = event.get("reason")
                    if reason is None and isinstance(event.get("event"), dict):
                        reason = event["event"].get("reason")
                elif hasattr(event, "reason"):
                    reason = event.reason
            except Exception:
                pass
            if hasattr(reason, "value"):
                reason = reason.value
            if str(reason).lower() in ("eof", "0", "b'eof'"):
                self._report_eof()

    def _report_eof(self) -> None:
        gen = self.generation
        if gen == self._eof_reported:
            return
        self._eof_reported = gen
        if self._eof_callback and self._loop:
            self._loop.call_soon_threadsafe(self._eof_callback, gen)

    def set_eof_callback(self, fn: Callable[[int], None]) -> None:
        self._eof_callback = fn

    # -- properties --------------------------------------------------------

    # Attribute access, not item access: python-mpv's mpv["name"] reads and
    # writes `options/<name>`, so runtime properties such as core-idle or
    # time-pos "do not exist" that way (the old player's wall-stream death
    # check failed on exactly that and never fired).
    def get(self, name: str, default=None):
        if not self._mpv:
            return default
        try:
            v = getattr(self._mpv, name.replace("-", "_"))
            return default if v is None else v
        except Exception:
            return default

    def set(self, name: str, value) -> None:
        if not self._mpv:
            return
        try:
            setattr(self._mpv, name.replace("-", "_"), value)
        except Exception as e:
            log.debug(f"mpv set {name}={value!r} failed: {e}")

    @property
    def time_pos(self) -> Optional[float]:
        return self.get("time-pos")

    @property
    def duration(self) -> Optional[float]:
        return self.get("duration")

    @property
    def core_idle(self) -> bool:
        return bool(self.get("core-idle", True))

    def display_size(self) -> tuple:
        w = self.get("display-width") or self.get("osd-width")
        h = self.get("display-height") or self.get("osd-height")
        try:
            w, h = int(w), int(h)
            if w > 0 and h > 0:
                return w, h
        except (TypeError, ValueError):
            pass
        return 1920, 1080

    # -- playback ----------------------------------------------------------

    def play_file(self, path: str, loop: bool = False, vf: Optional[str] = None,
                  start: Optional[float] = None, end: Optional[float] = None,
                  paused: bool = False) -> int:
        """Load and play a file or URL; returns this load's generation.
        start/end are mpv's per-load trim (clip in/out); None clears them."""
        if not self._mpv:
            return self.generation
        self.generation += 1
        self.set("loop-file", "inf" if loop else "no")
        self.set("vf", vf or "")
        self.set("keepaspect", not vf)
        self.set("start", f"{start:.3f}" if start else "none")
        self.set("end", f"{end:.3f}" if end is not None else "none")
        self.set("pause", paused)
        try:
            self._mpv.play(path)
        except Exception as e:
            log.error(f"mpv could not play {path}: {e}")
        return self.generation

    def show_still(self, path: str) -> int:
        """A full-screen picture (player screens) that never reports EOF."""
        return self.play_file(path, paused=True)

    def set_filter(self, vf: Optional[str]) -> None:
        self.set("vf", vf or "")
        self.set("keepaspect", not vf)

    def seek(self, seconds: float) -> None:
        if self._mpv:
            try:
                self._mpv.command("seek", f"{seconds:.3f}", "absolute")
            except Exception as e:
                log.debug(f"mpv seek failed: {e}")

    def pause(self, paused: bool) -> None:
        self.set("pause", paused)

    def set_volume(self, volume: int, muted: bool) -> None:
        self.set("volume", max(0, min(100, int(volume))))
        self.set("mute", bool(muted))

    def stop(self) -> None:
        if self._mpv:
            self.generation += 1
            try:
                self._mpv.command("stop")
            except Exception:
                pass

    def screenshot(self, path: Path) -> bool:
        if not self._mpv:
            return False
        try:
            self._mpv.command("screenshot-to-file", str(path), "video")
            return path.exists()
        except Exception as e:
            log.debug(f"[PREVIEW] mpv screenshot failed: {e}")
            return False

    def show_osd(self, text: str, duration_ms: int = 10_000) -> None:
        if not self._mpv:
            return
        try:
            self._mpv.command("osd-overlay", 1, "ass-events",
                              r"{\an5\fs48\bord3\b1}" + text.replace("\n", r"\N"))
            return
        except Exception:
            pass
        try:
            self._mpv.command("show-text", text, str(duration_ms))
        except Exception:
            pass

    def clear_osd(self) -> None:
        if not self._mpv:
            return
        try:
            self._mpv.command("osd-overlay", 1, "none", "")
        except Exception:
            pass

    def terminate(self) -> None:
        if self._mpv:
            try:
                self._mpv.terminate()
            except Exception:
                pass
            self._mpv = None


# ---------------------------------------------------------------------------
# Playlist engine
# ---------------------------------------------------------------------------

IDLE, WAITING, PLAYING, PAUSED, STOPPED = "idle", "waiting", "playing", "paused", "stopped"
SUSPENDED = "suspended"   # the screen is lent to a wall stream or an NDI window


class PlaylistEngine:
    """Owns what is on screen. `mpv` and `browser` are injected so tests can
    use fakes; `resolve(item)` finds an item's cached file (or None)."""

    def __init__(self, mpv, browser, *, default_image: float,
                 resolve: Callable[[Item], Optional[Path]],
                 blank: Callable[[], None],
                 on_waiting: Callable[[], None],
                 on_item: Optional[Callable[[Item], None]] = None,
                 screen_size: Callable[[], tuple] = lambda: (1920, 1080),
                 crop_filter: Callable[[Optional[tuple]], Optional[str]] = lambda c: None,
                 clock=time.monotonic):
        self.mpv = mpv
        self.browser = browser
        self.default_image = default_image
        self._resolve = resolve
        self._blank = blank
        self._on_waiting = on_waiting
        self._on_item = on_item
        self._screen_size = screen_size
        self._crop_filter = crop_filter
        self._clock = clock

        self.items: List[Item] = []
        self.index = -1
        self.mode = IDLE
        self.crop: Optional[tuple] = None
        self._timer: Optional[asyncio.Task] = None
        self._timer_ends = 0.0
        self._timer_remaining: Optional[float] = None
        self._gen = -1
        self._showing_image = False

    # -- queries -----------------------------------------------------------

    @property
    def has_content(self) -> bool:
        return bool(self.items)

    @property
    def current(self) -> Optional[Item]:
        if 0 <= self.index < len(self.items):
            return self.items[self.index]
        return None

    def ready(self, item: Item) -> bool:
        if item.is_media:
            found = self._resolve(item)
            if found is None:
                return False
            item.path = found
        return True

    def unready(self) -> List[Item]:
        return [it for it in self.items if not self.ready(it)]

    def status(self) -> dict:
        item = self.current
        on_screen_media = (item is not None and item.kind == VIDEO and not self._showing_image
                           and self.mode in (PLAYING, PAUSED))
        position = duration = None
        if on_screen_media:
            pos = self.mpv.time_pos
            natural = self.mpv.duration
            out = item.out_point if item.out_point is not None else natural
            if pos is not None:
                position = round(max(0.0, pos - item.in_point), 1)
            if out is not None:
                duration = round(max(0.0, out - item.in_point), 1)
        playback = {PLAYING: "playing", PAUSED: "paused"}.get(self.mode, "stopped")
        return {
            "status": playback if playback != "stopped" else "online",
            "playback_status": playback,
            "position": position,
            "duration": duration,
            "current_content_id": item.content_id if item else None,
            "current_content_name": item.name if item else None,
            "playlist_index": self.index if item else None,
            "playlist_total": len(self.items),
        }

    # -- timers ------------------------------------------------------------

    def _cancel_timer(self) -> None:
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None
        self._timer_remaining = None

    def _arm(self, seconds: float) -> None:
        self._cancel_timer()
        if seconds <= 0 or len(self.items) <= 1:
            return
        self._timer_ends = self._clock() + seconds
        self._timer = asyncio.ensure_future(self._timer_then_advance(seconds))

    async def _timer_then_advance(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        await self.advance()

    # -- loading -----------------------------------------------------------

    async def load(self, items: List[Item]) -> bool:
        """Replace the playlist. An identical re-apply keeps the current
        position (re-applying would restart it from item 0). Returns True when
        it replaced."""
        if same_content(self.items, items) and self.mode in (PLAYING, PAUSED, WAITING):
            if self.mode == WAITING:
                await self.kick()
            return False
        self.items = list(items)
        self.index = -1
        if not self.items:
            await self.release()
            return True
        if self.mode in (STOPPED, SUSPENDED):
            self.mode = IDLE   # new content ends a stop or a lent screen
        await self._start_from(0)
        return True

    async def release(self) -> None:
        """Drop everything (refresh's first step, re-enrollment)."""
        self._cancel_timer()
        await self.browser.stop()
        self.mpv.stop()
        self.items = []
        self.index = -1
        self.mode = IDLE

    async def kick(self) -> None:
        """Content may have landed: start if we were waiting for it."""
        if self.mode == WAITING and self.items:
            await self._start_from(0)

    async def suspend(self) -> None:
        """Lend the screen (wall stream, NDI): keep the playlist, stop showing it."""
        self._cancel_timer()
        await self.browser.stop()
        self.mode = SUSPENDED

    async def resume(self) -> None:
        """Take the screen back after a suspend."""
        if self.mode != SUSPENDED:
            return
        self.mode = IDLE
        if self.items:
            await self._start_from(max(0, self.index))
        else:
            self._on_waiting()

    async def _start_from(self, start: int, step: int = 1) -> None:
        n = len(self.items)
        for k in range(n):
            i = (start + step * k) % n
            if self.ready(self.items[i]):
                await self._play(i)
                return
        self._cancel_timer()
        await self.browser.stop()
        self.index = -1
        self.mode = WAITING
        self._on_waiting()

    async def _play(self, idx: int, force: bool = False) -> None:
        self._cancel_timer()
        item = self.items[idx]
        self.index = idx
        self.mode = PLAYING
        single = len(self.items) == 1

        if item.is_media:
            await self.browser.stop()
            is_img = item.kind == IMAGE or cachemod.is_image_file(item.path)
            self._showing_image = is_img
            vf = self._crop_filter(self.crop)
            if is_img:
                self._gen = self.mpv.play_file(str(item.path), vf=vf)
                self._arm(item.dwell(self.default_image))
            else:
                self._gen = self.mpv.play_file(
                    str(item.path), loop=single, vf=vf,
                    start=item.clip_start, end=item.out_point)
            log.info(f"Playing [{idx + 1}/{len(self.items)}] {item.name}"
                     f"{' (clip)' if item.is_clipped else ''}{' (wall crop)' if vf else ''}")
        else:
            self._showing_image = False
            if not (single and not force and self.browser.showing(item.url)):
                self._blank()
                scale = self._scale_for(item)
                ok = await self.browser.show(item.url, scale)
                if not ok:
                    log.error(f"Cannot show {item.kind} {item.name}: no browser")
                    if not single:
                        self._arm(1.0)
                    if self._on_item:
                        self._on_item(item)
                    return
            log.info(f"Showing {item.kind} [{idx + 1}/{len(self.items)}] {item.name}")
            self._arm(item.dwell(self.default_image))
        if self._on_item:
            self._on_item(item)

    def _scale_for(self, item: Item) -> Optional[float]:
        cw, ch = item.canvas
        if item.kind != TEMPLATE or cw <= 0 or ch <= 0:
            return None
        sw, sh = self._screen_size()
        return max(0.1, min(4.0, min(sw / cw, sh / ch)))

    # -- transport ---------------------------------------------------------

    async def advance(self) -> None:
        if not self.items:
            return
        if len(self.items) == 1 and self.mode == PLAYING and self.index == 0:
            return   # a single item never advances
        await self._start_from((self.index + 1) % len(self.items))

    async def previous(self) -> None:
        if not self.items:
            return
        await self._start_from((self.index - 1) % len(self.items), step=-1)

    def on_eof(self, gen: int) -> None:
        """mpv reached the end of the file loaded as generation `gen`."""
        if gen != self._gen or self.mode != PLAYING:
            return
        item = self.current
        if item is None or self._showing_image or len(self.items) <= 1:
            return
        asyncio.ensure_future(self.advance())

    def pause(self) -> None:
        if self.mode != PLAYING:
            return
        self.mpv.pause(True)
        if self._timer and not self._timer.done():
            remaining = max(0.0, self._timer_ends - self._clock())
            self._cancel_timer()
            self._timer_remaining = remaining
        self.mode = PAUSED

    async def play(self) -> None:
        if self.mode == PAUSED:
            self.mpv.pause(False)
            self.mode = PLAYING
            if self._timer_remaining is not None:
                remaining = self._timer_remaining
                self._arm(remaining)
        elif self.mode == STOPPED and self.items:
            self.mode = IDLE
            await self._start_from(max(0, self.index))
        elif self.mode == WAITING:
            await self.kick()

    async def stop(self) -> None:
        """Blank, and stay blank until play / replay / next / new content."""
        self._cancel_timer()
        await self.browser.stop()
        self.mpv.stop()
        self._blank()
        self.mode = STOPPED

    async def replay(self) -> tuple:
        if not self.items or self.index < 0:
            return False, "nothing playing"
        self.mode = PLAYING
        await self._play(self.index, force=True)
        return True, "replaying"

    def seek(self, position: Optional[float], seconds: Optional[float]) -> tuple:
        item = self.current
        if item is None or self.mode not in (PLAYING, PAUSED):
            return False, "nothing playing"
        if position is None and seconds is None:
            return False, "seek needs 'seconds' or 'position'"
        if item.kind != VIDEO or self._showing_image:
            return False, "not seekable"
        natural = self.mpv.duration
        out = item.out_point if item.out_point is not None else natural
        if out is None:
            return False, "not seekable"
        length = max(0.0, out - item.in_point)
        if position is not None:
            target = position
        else:
            now = (self.mpv.time_pos or item.in_point) - item.in_point
            target = now + seconds
        target = max(0.0, min(target, max(0.0, length - 0.1)))
        self.mpv.seek(item.in_point + target)
        return True, f"seeked to {target:.0f}s of {length:.0f}s"

    def set_crop(self, crop: Optional[tuple]) -> None:
        """Wall crop (normalised x, y, w, h) for media items; None clears."""
        if crop == self.crop:
            return
        self.crop = crop
        item = self.current
        if item is not None and item.is_media and self.mode in (PLAYING, PAUSED):
            self.mpv.set_filter(self._crop_filter(crop))

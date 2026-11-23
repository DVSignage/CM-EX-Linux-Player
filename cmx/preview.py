"""
Pushed live preview (DVSI Plus 1.5.5).

A cloud CMS cannot pull a preview from a screen behind a venue's router, so
the heartbeat says `preview_watch`: "thumb" (a Players page card) or "live"
(an open remote). While it does, JPEG frames are POSTed to
`/players/{id}/preview-frame`. The loop stops when the CMS answers
`watching: false` or no beat has said it for 10 s; a refused frame backs off
30 s.
"""

import asyncio
import io
import logging
import time
from typing import Awaitable, Callable, Optional

log = logging.getLogger("player")

MODES = {
    "thumb": ((640, 360), 60, 2.0),
    "live": ((960, 540), 70, 1.0),
}
HINT_TIMEOUT = 10.0
REFUSED_BACKOFF = 30.0


def encode_jpeg(image, size, quality: int) -> bytes:
    img = image.convert("RGB").resize(size)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


class PreviewPusher:
    def __init__(self, capture: Callable[[], Awaitable[Optional[object]]],
                 post: Callable[[bytes], Awaitable[Optional[bool]]],
                 clock=time.monotonic):
        self._capture = capture    # → PIL image or None
        self._post = post          # → watching (bool) or None if refused
        self._clock = clock
        self._mode: Optional[str] = None
        self._last_hint = 0.0
        self._task: Optional[asyncio.Task] = None
        self._blocked_until = 0.0

    def on_hint(self, mode) -> None:
        """Called with every heartbeat's `preview_watch` (None when absent)."""
        if mode not in MODES:
            return
        self._mode = mode
        self._last_hint = self._clock()
        if self._clock() < self._blocked_until:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        log.info(f"[PREVIEW] pushing frames ({self._mode})")
        try:
            while self._mode in MODES and self._clock() - self._last_hint < HINT_TIMEOUT:
                size, quality, interval = MODES[self._mode]
                started = self._clock()
                img = await self._capture()
                if img is not None:
                    jpeg = await asyncio.to_thread(encode_jpeg, img, size, quality)
                    watching = await self._post(jpeg)
                    if watching is None:
                        log.info("[PREVIEW] frame refused; backing off 30 s")
                        self._blocked_until = self._clock() + REFUSED_BACKOFF
                        return
                    if not watching:
                        log.info("[PREVIEW] nobody watching; stopped")
                        self._mode = None
                        return
                await asyncio.sleep(max(0.05, interval - (self._clock() - started)))
        except asyncio.CancelledError:
            pass
        finally:
            log.debug("[PREVIEW] loop ended")

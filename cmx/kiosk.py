"""
The Chromium kiosk that shows the renderer page, and its recovery.

A browser that exits, or a page that stops reporting in (hung renderer), is
relaunched with a growing delay, and each recovery is reported — the Linux
counterpart of DVSI Plus's WebView2 renderer recovery (`renderer_crash`).
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable, List, Optional

from .browser import FLAGS, find_chromium, profile_dir_for, root_flags
from .cdp import PORT as CDP_PORT

log = logging.getLogger("player")

KIOSK_FLAGS = FLAGS + [
    f"--remote-debugging-port={CDP_PORT}",   # loopback only: preview screenshots (cmx/cdp.py)
    "--use-fake-ui-for-media-stream",        # templates' capture elements: grant the camera
    "--overscroll-history-navigation=0",
    "--disable-pinch",
    "--password-store=basic",                # no keyring prompt on a signage box
    "--disable-features=TranslateUI,MediaRouter",
]
BACKOFF = [1, 2, 5, 10, 30]
HANG_SECONDS = 25.0          # no "alive" from the page for this long = hung
STARTUP_GRACE = 30.0
WINDOW = 300.0               # recoveries counted over five minutes
FAULT_AFTER = 3


class Kiosk:
    def __init__(self, display: str, profile_dir: Path, url: str,
                 on_recovery: Optional[Callable[[dict], None]] = None, clock=time.monotonic):
        self.display = display
        self.profile_dir = profile_dir
        self.url = url
        self._on_recovery = on_recovery
        self._clock = clock
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._last_alive = 0.0
        self._started_at = 0.0
        self._recoveries: List[float] = []
        self.last_error: Optional[str] = None

    @property
    def available(self) -> bool:
        return find_chromium() is not None

    @property
    def recoveries_in_window(self) -> int:
        now = self._clock()
        self._recoveries = [t for t in self._recoveries if now - t < WINDOW]
        return len(self._recoveries)

    @property
    def failing(self) -> bool:
        return self.recoveries_in_window >= FAULT_AFTER

    def alive(self) -> None:
        self._last_alive = self._clock()

    def start(self) -> None:
        self._stopping = False
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._supervise())

    async def stop(self) -> None:
        self._stopping = True
        if self._task and not self._task.done():
            self._task.cancel()
        await self._kill()

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass

    async def _launch(self) -> bool:
        exe = find_chromium()
        if not exe:
            self.last_error = "no Chromium browser installed"
            return False
        profile = profile_dir_for(exe, self.profile_dir)
        args = KIOSK_FLAGS + root_flags() + [f"--display={self.display}", f"--user-data-dir={profile}", self.url]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe, *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        except Exception as e:
            self.last_error = str(e)
            log.error(f"[KIOSK] could not launch Chromium: {e}")
            return False
        self._started_at = self._last_alive = self._clock()
        self.last_error = None
        log.info(f"[KIOSK] Chromium showing {self.url}")
        return True

    async def _supervise(self) -> None:
        attempt = 0
        while not self._stopping:
            if not await self._launch():
                await asyncio.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                attempt += 1
                continue
            kind, reason, code = await self._watch()
            if self._stopping:
                return
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            self._recoveries.append(self._clock())
            log.warning(f"[KIOSK] renderer {kind} ({reason}); relaunching in {delay}s")
            if self._on_recovery:
                try:
                    self._on_recovery({"surface": "renderer", "kind": kind, "reason": reason, "exit_code": code,
                                       "recovery": "relaunch", "delay_ms": delay * 1000,
                                       "recoveries_in_window": self.recoveries_in_window})
                except Exception:
                    pass
            await asyncio.sleep(delay)
            # a browser that stayed up for a while starts the backoff again
            attempt = 0 if self._clock() - self._started_at > WINDOW else attempt + 1

    async def _watch(self):
        """Wait until the browser exits or the page hangs: (kind, reason, exit code)."""
        proc = self._proc
        while True:
            try:
                code = await asyncio.wait_for(proc.wait(), timeout=2)
                return "process_exited", f"exit code {code}", code
            except asyncio.TimeoutError:
                pass
            now = self._clock()
            if now - self._started_at > STARTUP_GRACE and now - self._last_alive > HANG_SECONDS:
                await self._kill()
                return "unresponsive", f"no report from the page for {HANG_SECONDS:.0f}s", None

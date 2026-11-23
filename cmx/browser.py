"""
Chromium in kiosk mode for web pages, RSS renders and CM:EX templates.

A template render is a fixed pixel canvas, so it is shown with
`--force-device-scale-factor=min(screen/canvas)`; a responsive page (a URL or
an RSS render) is shown unscaled.
"""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional

log = logging.getLogger("player")

CANDIDATES = ("chromium-browser", "chromium", "google-chrome-stable", "google-chrome")

FLAGS = [
    "--kiosk",
    "--noerrdialogs",
    "--disable-infobars",
    "--no-first-run",
    "--disable-translate",
    "--disable-features=TranslateUI",
    "--disable-notifications",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--block-new-web-contents",
    "--disable-background-networking",
    "--disable-client-side-phishing-detection",
    "--disable-component-update",
    "--autoplay-policy=no-user-gesture-required",
    "--check-for-update-interval=31536000",
]


def find_chromium() -> Optional[str]:
    for name in CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


class Browser:
    def __init__(self, display: str, profile_dir: Path):
        self.display = display
        self.profile_dir = profile_dir
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._url: Optional[str] = None
        self.last_error: Optional[str] = None

    def showing(self, url: Optional[str]) -> bool:
        return self._proc is not None and self._proc.returncode is None and self._url == url

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def show(self, url: str, scale: Optional[float] = None) -> bool:
        await self.stop()
        exe = find_chromium()
        if not exe:
            self.last_error = "no Chromium browser installed"
            return False
        args = list(FLAGS) + [f"--display={self.display}", f"--user-data-dir={self.profile_dir}"]
        if scale:
            args.append(f"--force-device-scale-factor={scale:.4f}")
        args.append(url)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe, *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as e:
            self.last_error = str(e)
            log.error(f"Failed to launch Chromium: {e}")
            self._proc = None
            return False
        self._url = url
        self.last_error = None
        return True

    async def stop(self) -> None:
        proc, self._proc, self._url = self._proc, None, None
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

"""
Publishing this screen as an NDI source — the Linux counterpart of DVSI Plus
`NdiOutput.cs` and `NdiOutputPolicy.cs`.

What is sent: the whole display, as DVSI Plus does (its BitBlt of the
composited screen), so web pages, videos, templates, live inputs and the
multicast wall window all appear. Frames come from
  - an X screen grab on an X11 session (everything on screen), or
  - a Chromium screencast (DevTools, loopback) on a Wayland session, where an
    X grab is black — that one carries the renderer page only;
  - mpv's own screenshot in mpv-only mode when the X grab fails.
The last frame is re-sent at the configured rate so receivers get a steady
stream even when the picture is still.

Who turns it on (NdiOutputPolicy): the screen's own setting
(`ndi.output: true`) or the CMS (`ndi_broadcast_active` on the heartbeat, or
the start/stop_ndi_broadcast commands). The CMS sends `false` on every beat
when nobody asked, so a CMS "off" stops only a broadcast the CMS started; the
CMS's name applies only while the CMS is asking.
"""

import base64
import io
import json
import logging
import os
import socket
import threading
import time
from typing import Callable, Optional, Tuple
from urllib.parse import urlsplit

from . import cdp

try:
    import numpy as np
except ImportError:   # NDI output needs numpy (requirements-ndi.txt)
    np = None

log = logging.getLogger("player")

DEFAULT_FPS = 30
MAX_WIDTH = 1920


def decide(local_on: bool, local_name: str, cms_wants: Optional[bool], cms_name: Optional[str]) -> Tuple[bool, str]:
    """(on, name) — DVSI Plus NdiOutputPolicy.Decide."""
    cms = cms_wants is True
    if not cms and not local_on:
        return False, local_name
    return True, (cms_name.strip() if cms and cms_name and cms_name.strip() else local_name)


def default_name() -> str:
    return f"CMX {socket.gethostname()}"


def to_bgra(image, max_width: int = MAX_WIDTH):
    """PIL image → (h, w, 4) uint8 BGRA, scaled down to max_width."""
    img = image.convert("RGBA")
    if max_width and img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)))
    arr = np.asarray(img)
    return np.ascontiguousarray(arr[..., [2, 1, 0, 3]])


# ---------------------------------------------------------------------------
# Frame sources
# ---------------------------------------------------------------------------

class XGrabSource:
    """The whole X display."""

    def __init__(self, display: str):
        self.display = display

    def grab(self):
        from PIL import ImageGrab
        return ImageGrab.grab(xdisplay=self.display)

    def close(self):
        pass


class MpvSource:
    """mpv's own picture (mpv-only mode, X grab unavailable)."""

    def __init__(self, mpv):
        self.mpv = mpv

    def grab(self):
        m = getattr(self.mpv, "_mpv", None)
        return m.screenshot_raw() if m is not None else None

    def close(self):
        pass


class ScreencastSource:
    """Chromium's own frames of the renderer page (Page.startScreencast over a
    loopback DevTools websocket), kept in the background; grab() returns the
    newest. Chromium sends a frame when the page repaints."""

    def __init__(self, page_prefix: str, port: int = cdp.PORT, max_width: int = MAX_WIDTH):
        self.page_prefix = page_prefix
        self.port = port
        self.max_width = max_width
        self._jpeg: Optional[bytes] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ndi-screencast")
        self._thread.start()

    def grab(self):
        with self._lock:
            jpeg = self._jpeg
        if not jpeg:
            return None
        from PIL import Image
        return Image.open(io.BytesIO(jpeg))

    def close(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as e:
                log.debug(f"[NDI OUT] screencast ended: {e}")
            self._stop.wait(2)

    def _session(self):
        ws = cdp._page_ws_url(self.port, self.page_prefix, 5)
        if not ws:
            return
        u = urlsplit(ws)
        sock = socket.create_connection((u.hostname, u.port or 80), timeout=10)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(1024)
                if not chunk:
                    return
                head += chunk
            if b" 101 " not in head.split(b"\r\n", 1)[0]:
                return
            cdp._send(sock, json.dumps({"id": 1, "method": "Page.startScreencast", "params": {
                "format": "jpeg", "quality": 80, "maxWidth": self.max_width, "maxHeight": 2160, "everyNthFrame": 1}}))
            n = 1
            while not self._stop.is_set():
                msg = json.loads(cdp._recv_message(sock))
                if msg.get("method") == "Page.screencastFrame":
                    p = msg.get("params") or {}
                    with self._lock:
                        self._jpeg = base64.b64decode(p.get("data") or "")
                    n += 1
                    cdp._send(sock, json.dumps({"id": n, "method": "Page.screencastFrameAck",
                                                "params": {"sessionId": p.get("sessionId")}}))
        finally:
            try:
                sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# The output
# ---------------------------------------------------------------------------

class NdiOutput:
    """Feeds an NDISender (ndi/engine.py) from a frame source at a steady rate."""

    def __init__(self, sender, make_source: Callable[[], object], fps: int = DEFAULT_FPS,
                 max_width: int = MAX_WIDTH):
        self.sender = sender
        self._make_source = make_source
        self.fps = max(1, min(60, int(fps or DEFAULT_FPS)))
        self.max_width = max_width
        self._thread: Optional[threading.Thread] = None
        self._run = False
        self.frames_sent = 0
        self.last_error: Optional[str] = None

    @property
    def active(self) -> bool:
        return self._run and self.sender is not None and self.sender.active

    @property
    def name(self) -> str:
        return self.sender.source_name if self.sender is not None else ""

    def start(self, name: str) -> Tuple[bool, str]:
        if np is None:
            return False, "numpy is not installed (requirements-ndi.txt)"
        if self.sender is None:
            return False, "NDI is not available on this player"
        if self.active and self.name == name:
            return True, f"already broadcasting as '{name}'"
        self.stop()
        if not self.sender.start(name):
            return False, "the NDI sender could not be created (NDI runtime missing?)"
        self._run = True
        self.frames_sent = 0
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ndi-output")
        self._thread.start()
        log.info(f"[NDI OUT] broadcasting this screen as '{name}' at {self.fps} fps")
        return True, f"broadcasting as '{name}'"

    def stop(self) -> None:
        was = self._run
        self._run = False
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self.sender is not None and self.sender.active:
            self.sender.stop()
        if was:
            log.info("[NDI OUT] broadcast stopped")

    def _loop(self) -> None:
        source = None
        frame = None
        interval = 1.0 / self.fps
        try:
            source = self._make_source()
            while self._run:
                started = time.monotonic()
                try:
                    img = source.grab()
                    if img is not None:
                        frame = to_bgra(img, self.max_width)
                    if frame is not None and self.sender.send_frame(frame, self.fps):
                        self.frames_sent += 1
                    self.last_error = None
                except Exception as e:
                    if self.last_error != str(e):
                        log.warning(f"[NDI OUT] frame failed: {e}")
                    self.last_error = str(e)
                    time.sleep(0.5)
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
        finally:
            if source is not None:
                source.close()

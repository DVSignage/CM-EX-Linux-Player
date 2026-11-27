"""
NDI sources in the renderer page: template `ndi` elements (DVSI Plus
`StartTemplateNdi`) and the full-screen `show_ndi` / `show_ndi_wall`.

A template's `ndi` element names a source on the network (props.sourceName,
as "MACHINE (Sender)"). The page shows it as an <img> on a local MJPEG stream
(`/ndi/stream?source=…` on the renderer server); this module receives the
source and turns its frames into JPEGs for that stream.

  - One receiver per source, however many elements (or nested templates)
    show it. A stream acquires its feed when it opens and releases it when
    the page closes the connection; the receiver stops LINGER seconds after
    the last release, so a crossfade between two templates with the same
    source does not reconnect.
  - As on DVSI Plus the receiver connects by name and keeps trying until the
    sender appears; until frames arrive (and when they stop for NO_SIGNAL
    seconds) the stream shows a "waiting" picture instead of going black.
  - Each stream scales to the size the element is drawn at and is capped at
    `fps`, so a small element on a small board costs little.
  - A video-wall screen asks for its region (`crop`, normalised x/y/w/h);
    only that part is scaled and encoded.
  - Video only: an NDI sender's sound is not played.
"""

import io
import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("player")

LINGER = 3.0
NO_SIGNAL = 5.0
RETRY = 2.0
DEFAULT_FPS = 25
MAX_EDGE = 1920
JPEG_QUALITY = 80

try:
    import numpy as np
except ImportError:   # optional, with ndi-python
    np = None

try:
    import cv2
except ImportError:   # optional; Pillow encodes without it
    cv2 = None


def source_for(props: dict) -> Optional[str]:
    """An NDI element's source, as the CMS editor writes it (DVSI NdiSourceFor)."""
    for k in ("sourceName", "source_name", "ndiSource"):
        v = props.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def match_source(wanted: str, names: List[str]) -> Optional[str]:
    """The exact name first, then case-insensitive, then a name containing
    it ("Sender" finds "MACHINE (Sender)")."""
    if wanted in names:
        return wanted
    low = wanted.lower()
    for n in names:
        if n.lower() == low:
            return n
    for n in names:
        if low in n.lower():
            return n
    return None


def fit_size(w: int, h: int, max_w: int, max_h: int) -> Tuple[int, int]:
    """Scale (w, h) down to fit max_w × max_h (never up); 0 means no limit."""
    max_w = min(max_w or MAX_EDGE, MAX_EDGE)
    max_h = min(max_h or MAX_EDGE, MAX_EDGE)
    s = min(1.0, max_w / w if w else 1.0, max_h / h if h else 1.0)
    return max(1, round(w * s)), max(1, round(h * s))


Crop = Tuple[float, float, float, float]


def parse_crop(x, y, w, h) -> Optional[Crop]:
    """A normalised region from query values; None when absent, empty or the whole frame."""
    try:
        x, y, w, h = (float(v) for v in (x, y, w, h))
    except (TypeError, ValueError):
        return None
    x, y = max(0.0, min(1.0, x)), max(0.0, min(1.0, y))
    w, h = min(w, 1.0 - x), min(h, 1.0 - y)
    if w <= 0 or h <= 0 or (x, y, w, h) == (0.0, 0.0, 1.0, 1.0):
        return None
    return x, y, w, h


def crop_frame(frame, crop: Optional[Crop]):
    """The part of a frame a normalised crop names (at least one pixel)."""
    if not crop:
        return frame
    h, w = frame.shape[:2]
    x1, y1 = int(crop[0] * w), int(crop[1] * h)
    x2, y2 = int(round((crop[0] + crop[2]) * w)), int(round((crop[1] + crop[3]) * h))
    x1, y1 = min(x1, w - 1), min(y1, h - 1)
    return frame[y1:max(y1 + 1, min(y2, h)), x1:max(x1 + 1, min(x2, w))]


def encode_jpeg(frame, max_w: int = 0, max_h: int = 0, quality: int = JPEG_QUALITY) -> bytes:
    """A BGRX/BGRA (or BGR) numpy frame as a JPEG, scaled to fit."""
    h, w = frame.shape[:2]
    tw, th = fit_size(w, h, max_w, max_h)
    bgr = frame[:, :, :3]
    if cv2 is not None:
        if (tw, th) != (w, h):
            bgr = cv2.resize(bgr, (tw, th), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if ok:
            return buf.tobytes()
    from PIL import Image
    img = Image.fromarray(np.ascontiguousarray(bgr[:, :, ::-1]), "RGB")
    if (tw, th) != (w, h):
        img = img.resize((tw, th), Image.BILINEAR)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


def placeholder_jpeg(title: str, detail: str = "", size: Tuple[int, int] = (640, 360)) -> bytes:
    """The slot's picture while there is nothing to show (DVSI "Waiting for signal")."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", size, (17, 17, 17))
    d = ImageDraw.Draw(img)

    def font(px):
        try:
            return ImageFont.load_default(size=px)
        except TypeError:   # Pillow < 10.1
            return ImageFont.load_default()

    for text, px, y, colour in ((title, 30, size[1] // 2 - 30, (230, 230, 230)),
                                (detail, 20, size[1] // 2 + 16, (150, 150, 150))):
        if not text:
            continue
        f = font(px)
        w = d.textlength(text, font=f)
        d.text(((size[0] - w) / 2, y), text, font=f, fill=colour)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


# ---------------------------------------------------------------------------
# The NDI runtime behind a small seam, so the feed logic is testable
# ---------------------------------------------------------------------------

class NdiBackend:
    """ndi-python: find sources, connect a receiver by name, read frames."""

    def __init__(self):
        import NDIlib as ndi   # noqa: N813 — the package's own name
        self.ndi = ndi
        self._finder = None
        self._found: list = []
        self._lock = threading.Lock()

    def sources(self, wait_ms: int = 1000) -> List[str]:
        with self._lock:
            if self._finder is None:
                self._finder = self.ndi.find_create_v2()
            if self._finder is None:
                return []
            self.ndi.find_wait_for_sources(self._finder, wait_ms)
            self._found = list(self.ndi.find_get_current_sources(self._finder))
            return [s.ndi_name for s in self._found]

    def connect(self, name: str):
        with self._lock:
            src = next((s for s in self._found if s.ndi_name == name), None)
        if src is None:
            return None
        create = self.ndi.RecvCreateV3()
        create.color_format = self.ndi.RECV_COLOR_FORMAT_BGRX_BGRA
        create.bandwidth = self.ndi.RECV_BANDWIDTH_HIGHEST
        recv = self.ndi.recv_create_v3(create)
        if recv is None:
            return None
        self.ndi.recv_connect(recv, src)
        return recv

    def capture(self, recv, timeout_ms: int = 200):
        """The next video frame (a numpy copy) or None; other frames are freed."""
        ndi = self.ndi
        t, v, a, m = ndi.recv_capture_v2(recv, timeout_ms)
        if t == ndi.FRAME_TYPE_VIDEO:
            frame = np.copy(v.data)
            ndi.recv_free_video_v2(recv, v)
            return frame
        if t == ndi.FRAME_TYPE_AUDIO and a is not None:
            ndi.recv_free_audio_v2(recv, a)
        elif t == ndi.FRAME_TYPE_METADATA and m is not None:
            ndi.recv_free_metadata(recv, m)
        return None

    def close(self, recv) -> None:
        try:
            self.ndi.recv_destroy(recv)
        except Exception:
            pass

    def shutdown(self) -> None:
        with self._lock:
            if self._finder is not None:
                try:
                    self.ndi.find_destroy(self._finder)
                except Exception:
                    pass
                self._finder = None


def default_backend() -> Optional[NdiBackend]:
    if np is None:
        return None
    try:
        from ndi import engine
        if not engine.is_available():
            return None
        return NdiBackend()
    except Exception as e:
        log.info(f"[NDI-IN] NDI runtime not usable: {e}")
        return None


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------

class Feed:
    """One source being received: the latest frame and its state."""

    def __init__(self, name: str):
        self.name = name
        self.users = 0
        self.connected_as: Optional[str] = None
        self.frame = None
        self.seq = 0
        self.last_frame = 0.0
        self.released_at: Optional[float] = None
        self.stop = threading.Event()
        self.cond = threading.Condition()
        self.thread: Optional[threading.Thread] = None
        self.frames = 0

    def state(self, now: float) -> str:
        if self.connected_as is None:
            return "waiting"
        if self.frame is None or now - self.last_frame > NO_SIGNAL:
            return "no_signal"
        return "receiving"

    def publish(self, frame, now: float) -> None:
        with self.cond:
            self.frame = frame
            self.seq += 1
            self.frames += 1
            self.last_frame = now
            self.cond.notify_all()

    def wait(self, seq: int, timeout: float) -> int:
        with self.cond:
            if self.seq == seq:
                self.cond.wait(timeout)
            return self.seq


class NdiFeeds:
    def __init__(self, backend: Optional[NdiBackend] = None, clock: Callable[[], float] = time.monotonic,
                 linger: float = LINGER, fps: int = DEFAULT_FPS):
        self.backend = backend
        self.clock = clock
        self.linger = linger
        self.fps = max(1, min(60, int(fps or DEFAULT_FPS)))
        self._feeds: Dict[str, Feed] = {}
        self._lock = threading.Lock()
        self._closed = False

    @property
    def available(self) -> bool:
        return self.backend is not None

    def active(self) -> List[dict]:
        now = self.clock()
        with self._lock:
            return [{"source": f.name, "connected_as": f.connected_as, "state": f.state(now), "viewers": f.users}
                    for f in self._feeds.values() if f.users > 0]

    def acquire(self, name: str) -> Optional[Feed]:
        if not self.backend or self._closed:
            return None
        with self._lock:
            self._reap_locked()
            feed = self._feeds.get(name)
            if feed is None or feed.stop.is_set():
                feed = Feed(name)
                self._feeds[name] = feed
                feed.thread = threading.Thread(target=self._run, args=(feed,), daemon=True, name=f"ndi-in {name}")
                feed.thread.start()
                log.info(f"[NDI-IN] receiving '{name}' for a template")
            feed.users += 1
            feed.released_at = None
            return feed

    def release(self, feed: Feed) -> None:
        with self._lock:
            feed.users = max(0, feed.users - 1)
            if feed.users == 0:
                feed.released_at = self.clock()
        if self.linger:
            t = threading.Timer(self.linger + 0.05, self.reap)
            t.daemon = True
            t.start()
        else:
            self.reap()

    def reap(self) -> None:
        with self._lock:
            self._reap_locked()

    def _reap_locked(self) -> None:
        now = self.clock()
        for name, f in list(self._feeds.items()):
            if f.users == 0 and f.released_at is not None and now - f.released_at >= self.linger:
                f.stop.set()
                with f.cond:
                    f.cond.notify_all()
                del self._feeds[name]
                log.info(f"[NDI-IN] stopped '{name}' ({f.frames} frames)")

    def close(self) -> None:
        self._closed = True
        with self._lock:
            feeds = list(self._feeds.values())
            self._feeds.clear()
        for f in feeds:
            f.stop.set()
            with f.cond:
                f.cond.notify_all()
        for f in feeds:
            if f.thread and f.thread is not threading.current_thread():
                f.thread.join(timeout=2)
        if self.backend:
            self.backend.shutdown()

    # -- the receive thread --------------------------------------------------

    def _run(self, feed: Feed) -> None:
        be = self.backend
        recv = None
        missing_logged = False
        try:
            while not feed.stop.is_set():
                if recv is None:
                    try:
                        names = be.sources(1000)
                    except Exception as e:
                        log.warning(f"[NDI-IN] finding sources failed: {e}")
                        names = []
                    found = match_source(feed.name, names)
                    if found:
                        recv = be.connect(found)
                    if recv is None:
                        if not missing_logged:
                            log.info(f"[NDI-IN] '{feed.name}' not on the network yet "
                                     f"(seen: {', '.join(names) or 'none'}) — waiting for it")
                            missing_logged = True
                        feed.stop.wait(RETRY)
                        continue
                    feed.connected_as = found
                    log.info(f"[NDI-IN] connected to '{found}'")
                try:
                    frame = be.capture(recv, 200)
                except Exception as e:
                    log.warning(f"[NDI-IN] '{feed.name}' receive failed: {e}")
                    feed.stop.wait(0.2)
                    continue
                if frame is not None:
                    feed.publish(frame, self.clock())
        finally:
            if recv is not None:
                be.close(recv)

    # -- one MJPEG stream ----------------------------------------------------

    def frames(self, feed: Feed, max_w: int = 0, max_h: int = 0, stop: Optional[threading.Event] = None,
               crop: Optional[Crop] = None):
        """JPEGs for one stream: a frame when a new one arrived (at most
        `fps` a second), the waiting picture while there is none. Ends when
        the feed stops or `stop` is set."""
        gap = 1.0 / self.fps
        seq = -1
        shown_state = None
        held = None
        while not feed.stop.is_set() and not (stop and stop.is_set()):
            now = self.clock()
            state = feed.state(now)
            if state == "receiving" and feed.seq != seq:
                seq = feed.seq
                frame = feed.frame
                try:
                    yield encode_jpeg(crop_frame(frame, crop), max_w, max_h)
                except Exception as e:
                    log.warning(f"[NDI-IN] encoding a frame of '{feed.name}' failed: {e}")
                shown_state = state
                time.sleep(gap)
                continue
            if state != "receiving":
                if state != shown_state or held is None:
                    shown_state = state
                    if state == "waiting":
                        held = placeholder_jpeg("Waiting for NDI source", feed.name)
                    else:
                        held = placeholder_jpeg("No signal", feed.connected_as or feed.name)
                # sent again every second as a keep-alive: a page that went away is noticed
                yield held
                feed.wait(feed.seq, 1.0)
            else:
                feed.wait(seq, 1.0)

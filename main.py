#!/usr/bin/env python3
"""
Digital Signage CMS - Linux Media Player

Single-file orchestrator that mirrors the Windows player (main.js) behaviour:
  - Enrollment flow with OSD code display via mpv
  - Boot-from-cache for instant playback
  - Content download from CMS API (/api/v1/content/{id}/stream)
  - Progressive playback (starts on first downloaded file)
  - Per-item durations (0 = play-to-end for video, >0 = timer)
  - 500 ms heartbeat with full command dispatch
  - 50 GB cache limit
  - state.json for player_id persistence
  - Graceful shutdown on SIGINT / SIGTERM
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import yaml

try:
    import mpv as _mpv_module
except ImportError:
    _mpv_module = None

# psutil — system metrics (optional — gracefully disabled if not installed)
try:
    import psutil as _psutil
except ImportError:
    _psutil = None

# NDI support (optional — gracefully disabled if not installed)
try:
    from ndi.engine import (
        is_available as ndi_available,
        find_sources as ndi_find_sources,
        NDIReceiver,
        NDISender,
        cleanup as ndi_cleanup,
    )
except ImportError:
    ndi_available = lambda: False
    ndi_find_sources = lambda **kw: []
    NDIReceiver = None
    NDISender = None
    ndi_cleanup = lambda: None

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("player")

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------
CONFIG_DIR = Path.home() / ".config" / "signage-player"
STATE_PATH = CONFIG_DIR / "state.json"
OFFLINE_PLAYLIST_PATH = CONFIG_DIR / "offline_playlist.json"
CACHE_DIR = CONFIG_DIR / "cache"
CACHE_LIMIT_BYTES = 50 * 1024 * 1024 * 1024  # 50 GB

# Per-tile MP4 cache for HTTP-served local-file wall playback.
# Each downloaded file is keyed by sha256(url)[:16].mp4 with an .etag
# sidecar; LRU-evicted past TILE_CACHE_MAX_FILES.
TILE_CACHE_DIR = CONFIG_DIR / "tile_cache"
TILE_CACHE_ETAG_DIR = TILE_CACHE_DIR / ".etags"
TILE_CACHE_MAX_FILES = 8

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"}
DEFAULT_IMAGE_DURATION = 10  # seconds when duration == 0 for an image
LOCAL_API_PORT = 8081  # Local HTTP API (same as Windows player)
PLAYER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Config loading (config.yaml)
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load config.yaml from standard search locations."""
    search = [
        Path("config.yaml"),
        Path("/etc/signage-player/config.yaml"),
        Path.home() / ".config" / "signage-player" / "config.yaml",
    ]
    for p in search:
        if p.exists():
            with open(p) as f:
                data = yaml.safe_load(f) or {}
            log.info(f"Loaded config from {p}")
            return data
    log.warning("No config.yaml found — using defaults.")
    return {}


# ---------------------------------------------------------------------------
# State persistence  (~/.config/signage-player/state.json)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception as e:
            log.error(f"Failed to load state: {e}")
    return {}


def save_state(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def clear_state() -> None:
    if STATE_PATH.exists():
        STATE_PATH.unlink()


# ---------------------------------------------------------------------------
# MPV wrapper
# ---------------------------------------------------------------------------

class MpvPlayer:
    """
    Thin wrapper around python-mpv that exposes the operations needed by
    the player orchestrator.

    Thread-safety note: mpv callbacks fire on mpv's internal thread.  We
    schedule coroutines back on the asyncio event loop via
    loop.call_soon_threadsafe() so all state mutations happen on the loop.
    """

    def __init__(self, display: str = ":0", audio_output: str = "auto"):
        if _mpv_module is None:
            raise RuntimeError(
                "python-mpv is not installed.  Run: pip install python-mpv"
            )
        self.display = display
        self.audio_output = audio_output
        self._mpv: Optional[_mpv_module.MPV] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._eof_callback = None   # async callable() — called when a file ends

    def initialise(self, loop: asyncio.AbstractEventLoop) -> None:
        """Create the mpv instance.  Must be called from the async main."""
        self._loop = loop
        # Guard flag: True while we're deliberately loading a new file so that
        # the end-file callback for the interrupted previous file is ignored.
        self._deliberate_load = False
        # Dedup guard: True once we've scheduled _on_eof for the current file.
        # Prevents a second end-file event (can occur with keep_open) from
        # scheduling a second advance before the first one completes.
        self._eof_pending = False

        kwargs = dict(
            input_default_bindings=False,
            input_vo_keyboard=False,
            osc=False,
            ytdl=False,
            idle=True,              # keep window open even with nothing playing
            keep_open="always",     # freeze last frame on EOF for smooth transitions;
                                    # the _eof_pending guard handles any spurious second
                                    # end-file event this can cause between videos
            force_window="yes",
            hwdec="auto",
            fs=True,                # fullscreen
            really_quiet=True,
            osd_level=0,            # suppress all automatic OSD (filename, buffering, seek bar, etc.)
            osd_font_size=48,
            osd_duration=10000,
            # Prevent display / screensaver blanking during playback
            stop_screensaver="yes",
            # Pre-buffer next item to reduce decode gap between files
            prefetch_playlist="yes",
            demuxer_readahead_secs=5,
        )

        # audio device
        if self.audio_output and self.audio_output != "auto":
            kwargs["audio_device"] = self.audio_output

        # Try GPU VO first, then fall back to safer alternatives
        for vo_driver in ["gpu", "x11", "drm", ""]:
            try:
                if vo_driver:
                    kwargs["vo"] = vo_driver
                self._mpv = _mpv_module.MPV(**kwargs)
                log.info(f"mpv initialised (vo={vo_driver or 'auto'}, fullscreen)")
                break
            except Exception as e:
                log.warning(f"mpv vo={vo_driver or 'auto'} failed: {e}")
                kwargs.pop("vo", None)
        else:
            raise RuntimeError("Could not initialise mpv with any video output driver")

        @self._mpv.event_callback("file-loaded")
        def _on_file_loaded(event):
            # New file has started — reset both guards for the fresh file
            self._deliberate_load = False
            self._eof_pending = False

        @self._mpv.event_callback("end-file")
        def _on_end_file(event):
            # If we triggered this end-file by calling play() on a new file,
            # ignore it — the deliberate_load flag tells us it was intentional.
            if self._deliberate_load:
                return

            # Extra safety: read the reason regardless of event format.
            # python-mpv >= 1.0 passes event as a dict; older versions pass an
            # MpvEvent struct (not a dict), which was the cause of the original
            # bug where reason was always None and ALL end-file events triggered
            # an advance, even intentional stop events.
            reason = None
            try:
                if isinstance(event, dict):
                    reason = event.get("reason")
                elif hasattr(event, "reason"):
                    reason = event.reason
                elif hasattr(event, "__getitem__"):
                    reason = event["reason"]
            except Exception:
                pass

            # Normalise enum values (python-mpv may return an EndOfFileReason enum)
            if hasattr(reason, "value"):
                reason = reason.value

            log.debug(f"end-file reason={reason!r} deliberate={self._deliberate_load} eof_pending={self._eof_pending}")

            # Only advance on natural end-of-file.
            # stop / quit / error / redirect must NOT trigger an advance.
            if str(reason).lower() in ("eof", "0") or reason == 0:
                # Dedup: only schedule _on_eof once per file play.
                # A second end-file with reason=eof can arrive when keep_open
                # transitions from the paused-at-end state to a new file.
                if self._eof_pending:
                    return
                self._eof_pending = True
                if self._eof_callback and self._loop:
                    self._loop.call_soon_threadsafe(
                        self._loop.create_task, self._eof_callback()
                    )

    def set_eof_callback(self, coro_fn) -> None:
        """Register an async callable to invoke when a file finishes playing."""
        self._eof_callback = coro_fn

    def play_file(self, path: str, loop: bool = False, vf: Optional[str] = None,
                  start_paused: bool = False) -> None:
        """Load and play a local file path or URL.

        Sets _deliberate_load=True before calling play() so the end-file
        callback for any currently-playing file is ignored (it was stopped
        intentionally, not via natural EOF).

        vf: optional mpv video filter string, e.g. "crop=960:1080:0:0".
            Pass None to clear any previously-set filter.
        start_paused: if True, load the file and begin buffering but do not
            start rendering — caller must set mpv.pause = False to begin.
        """
        if self._mpv:
            self._deliberate_load = True
            try:
                self._mpv.loop_file = "inf" if loop else "no"
            except Exception:
                pass
            # Apply or clear video filter (crop for wall mode)
            try:
                self._mpv.vf = vf if vf else ""
            except Exception:
                pass
            # Disable aspect ratio correction when a crop filter is active
            # so mpv doesn't add letterboxing after our scale+crop filter chain
            try:
                self._mpv.keepaspect = False if vf else True
            except Exception:
                pass
            self._mpv.play(path)
            self._mpv.pause = start_paused

    def show_osd(self, text: str, duration_ms: int = 10_000) -> None:
        """Display OSD text overlay.

        Tries multiple methods in order of reliability:
        1. osd-overlay (mpv >= 0.31, persistent, supports ASS styling)
        2. show-text command (works in most versions)
        3. osd_msg1 property (last resort)
        """
        if not self._mpv:
            return
        # Method 1: osd-overlay (most reliable for persistent text)
        try:
            self._mpv.command(
                "osd-overlay", 1, "ass-events",
                # ASS-formatted text: centred, white on semi-transparent black box
                r"{\an5\fs48\bord3\b1}" + text.replace("\n", r"\N")
            )
            return
        except Exception:
            pass
        # Method 2: show-text
        try:
            self._mpv.command("show-text", text, str(duration_ms))
            return
        except Exception:
            pass
        # Method 3: property
        try:
            self._mpv.osd_msg1 = text
        except Exception:
            pass

    def clear_osd(self) -> None:
        """Remove any persistent OSD overlay."""
        if not self._mpv:
            return
        try:
            self._mpv.command("osd-overlay", 1, "none", "")
        except Exception:
            pass
        try:
            self._mpv.osd_msg1 = ""
        except Exception:
            pass

    def cmd_play(self) -> None:
        if self._mpv:
            self._mpv.pause = False

    def cmd_pause(self) -> None:
        if self._mpv:
            self._mpv.pause = True

    def cmd_stop(self) -> None:
        if self._mpv:
            try:
                self._mpv.command("stop")
            except Exception:
                pass

    def cmd_seek_start(self) -> None:
        """Seek to the beginning of the current file (restart)."""
        if self._mpv:
            try:
                self._mpv.command("seek", "0", "absolute")
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
# Helper — is a filename an image?
# ---------------------------------------------------------------------------

def _is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def _find_chromium() -> Optional[str]:
    """Return path to a Chromium-compatible browser binary, or None."""
    for candidate in ("chromium-browser", "chromium", "google-chrome-stable", "google-chrome"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def _build_crop_filter(crop: Optional[dict]) -> Optional[str]:
    """Convert wall_crop dict {x, y, w, h, canvas_w, canvas_h} to an mpv filter string.

    Scales the source to the full canvas size (force-stretching to fill,
    no letterboxing), resets SAR so mpv doesn't re-apply aspect correction
    at the output stage, then crops the portion assigned to this display.

    mpv filter syntax: scale=W:H:force_original_aspect_ratio=disable,setsar=1,crop=W:H:X:Y
    Returns None if crop is absent or invalid.
    """
    if not crop:
        return None
    w = int(crop.get("w", 0))
    h = int(crop.get("h", 0))
    x = int(crop.get("x", 0))
    y = int(crop.get("y", 0))
    canvas_w = int(crop.get("canvas_w", 0))
    canvas_h = int(crop.get("canvas_h", 0))
    if w > 0 and h > 0:
        if canvas_w > 0 and canvas_h > 0:
            return (
                f"scale={canvas_w}:{canvas_h}"
                f":force_original_aspect_ratio=disable"
                f",setsar=1"
                f",crop={w}:{h}:{x}:{y}"
            )
        return f"crop={w}:{h}:{x}:{y}"
    return None


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_size_bytes() -> int:
    total = 0
    try:
        for f in CACHE_DIR.iterdir():
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    return total


# ---------------------------------------------------------------------------
# Local IP detection
# ---------------------------------------------------------------------------

def _get_local_ip() -> str:
    """Get the machine's LAN IP address."""
    import socket
    try:
        # Connect to a public IP (doesn't actually send data) to determine
        # which local interface is used for outbound traffic.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Local API server (mirrors Windows player api.js — port 8081)
# ---------------------------------------------------------------------------

class LocalApiServer:
    """
    Lightweight HTTP server that runs on a background thread.
    Serves endpoints that the CMS proxies to (NDI sources, preview, etc.)
    """

    PREVIEW_PATH = CONFIG_DIR / "preview.jpg"

    def __init__(self):
        self._server = None
        self._thread = None
        self._player = None  # Set via set_player()

    def set_player(self, player) -> None:
        """Give the API server a reference to the Player for preview captures."""
        self._player = player

    def _capture_screenshot(self) -> Optional[bytes]:
        """Capture current mpv frame as JPEG bytes."""
        if not self._player or not self._player.mpv._mpv:
            return None
        try:
            tmp_path = str(self.PREVIEW_PATH)
            self._player.mpv._mpv.command(
                "screenshot-to-file", tmp_path, "video"
            )
            # Read and return
            if self.PREVIEW_PATH.exists():
                data = self.PREVIEW_PATH.read_bytes()
                return data
        except Exception as e:
            log.debug(f"[PREVIEW] Screenshot failed: {e}")
        return None

    def start(self) -> None:
        """Start the local API server on LOCAL_API_PORT."""
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import threading

        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                # Suppress default stderr logging
                pass

            def _send_json(self, data, status=200):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/ndi/sources":
                    try:
                        sources = ndi_find_sources(timeout_ms=3000)
                        self._send_json(sources)
                    except Exception as e:
                        self._send_json({"error": str(e)}, 500)

                elif self.path == "/api/preview/snapshot":
                    jpeg_data = server_ref._capture_screenshot()
                    if jpeg_data:
                        self.send_response(200)
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Content-Length", str(len(jpeg_data)))
                        self.end_headers()
                        self.wfile.write(jpeg_data)
                    else:
                        self._send_json(
                            {"error": "Unable to capture frame"}, 503
                        )

                elif self.path == "/api/preview/stream":
                    # MJPEG stream — push frames continuously
                    self.send_response(200)
                    self.send_header(
                        "Content-Type",
                        "multipart/x-mixed-replace; boundary=frame",
                    )
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    try:
                        while True:
                            jpeg_data = server_ref._capture_screenshot()
                            if jpeg_data:
                                self.wfile.write(b"--frame\r\n")
                                self.wfile.write(
                                    b"Content-Type: image/jpeg\r\n"
                                )
                                self.wfile.write(
                                    f"Content-Length: {len(jpeg_data)}\r\n\r\n".encode()
                                )
                                self.wfile.write(jpeg_data)
                                self.wfile.write(b"\r\n")
                                self.wfile.flush()
                            time.sleep(0.333)  # ~3 FPS
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # Client disconnected

                elif self.path == "/api/device":
                    ip = _get_local_ip()
                    self._send_json({
                        "ip_address": ip,
                        "platform": "linux",
                        "ndi_available": ndi_available(),
                        "preview_supported": True,
                    })

                elif self.path == "/api/health":
                    self._send_json({"status": "ok"})

                else:
                    self._send_json({"error": "Not found"}, 404)

            def do_OPTIONS(self):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

        try:
            self._server = HTTPServer(("0.0.0.0", LOCAL_API_PORT), Handler)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="local-api"
            )
            self._thread.start()
            log.info(f"Local API server listening on port {LOCAL_API_PORT}")
        except Exception as e:
            log.error(f"Failed to start local API server: {e}")

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------



def _tile_cache_key(url: str) -> str:
    """Stable short key for a tile URL. Same URL across pushes -> same key,
    so the ETag short-circuit fires and the player avoids re-downloading."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _download_tile_with_etag(url: str, timeout: float = 30.0) -> Optional[Path]:
    """Sync HTTP GET with If-None-Match support. Returns local path on success.

    On 304: returns the existing cached file (no network bytes).
    On 200: streams to a .part file, atomically renames into place, writes
            the new ETag sidecar.
    On error: returns None (caller logs and abandons this push cycle).

    Caller MUST run this in asyncio.to_thread() — it's blocking IO.
    """
    import urllib.request, urllib.error
    TILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TILE_CACHE_ETAG_DIR.mkdir(parents=True, exist_ok=True)
    key = _tile_cache_key(url)
    file_path = TILE_CACHE_DIR / f"{key}.mp4"
    etag_path = TILE_CACHE_ETAG_DIR / f"{key}.etag"

    headers = {}
    if etag_path.exists() and file_path.exists() and file_path.stat().st_size > 1024:
        try:
            headers["If-None-Match"] = etag_path.read_text().strip()
        except Exception:
            pass

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            new_etag = resp.headers.get("ETag")
            tmp = file_path.with_suffix(".mp4.part")
            n = 0
            with tmp.open("wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    n += len(chunk)
            tmp.replace(file_path)
            if new_etag:
                etag_path.write_text(new_etag)
            log.info(f"[WALL LOCAL] downloaded {n//1024} KB in {time.time()-t0:.2f}s {file_path.name}")
    except urllib.error.HTTPError as e:
        if e.code == 304 and file_path.exists():
            log.info(f"[WALL LOCAL] cache HIT (304) {file_path.name} ({file_path.stat().st_size//1024} KB)")
            return file_path
        log.error(f"[WALL LOCAL] HTTP {e.code} on {url}")
        return None
    except Exception as e:
        log.error(f"[WALL LOCAL] download failed: {e}")
        return None

    _evict_lru_tile_cache()
    return file_path


def _evict_lru_tile_cache() -> None:
    """Keep at most TILE_CACHE_MAX_FILES MP4s in the tile cache. Drops oldest
    by mtime (and the matching .etag sidecar)."""
    try:
        files = sorted(TILE_CACHE_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        for p in files[:-TILE_CACHE_MAX_FILES]:
            try:
                p.unlink()
                etag = TILE_CACHE_ETAG_DIR / (p.stem + ".etag")
                if etag.exists():
                    etag.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _ffprobe_duration(path: Path) -> float:
    """Return container duration in seconds via ffprobe. 0.0 on failure."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(out.stdout.strip()) if out.returncode == 0 else 0.0
    except Exception:
        return 0.0


class Player:
    """
    Full Linux player that mirrors windows player/main.js behaviour.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.cms_url: str = cfg.get("cms_url", "").rstrip("/")
        self.state: dict = load_state()

        # Ensure cms_url is stored / updated in state if config provides it
        if self.cms_url:
            self.state["cms_url"] = self.cms_url
            save_state(self.state)

        # Playback settings from config
        pb = cfg.get("playback", {})
        self.display: str = pb.get("display", ":0")
        self.audio_output: str = pb.get("audio_output", "auto")

        # mpv engine
        self.mpv = MpvPlayer(display=self.display, audio_output=self.audio_output)

        # Deduplication / boot globals  (mirror JS globals)
        self.last_playlist_hash: Optional[str] = None
        self.last_content_id: Optional[str] = None
        self.boot_cache_loaded: bool = False

        # Wall playlist mode (deprecated — server now streams everything)
        self._wall_crop: Optional[dict] = None

        # Current playlist state
        self._playlist_items: list = []   # list of {path, duration, filename}
        self._current_index: int = -1
        self._duration_timer: Optional[asyncio.Task] = None

        # Control flags
        self._running = True
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._enrollment_task: Optional[asyncio.Task] = None

        # httpx client (shared)
        self._http: Optional[httpx.AsyncClient] = None

        # Chromium process for template display
        self._chromium_proc: Optional[asyncio.subprocess.Process] = None

        # asyncio event loop (set in run())
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Player start time (for uptime reporting)
        self._start_time: float = time.time()

        # NDI state
        self._ndi_receiver: Optional[object] = None
        self._ndi_sender: Optional[object] = None
        self._ndi_active_key: Optional[str] = None  # dedup key for wall commands
        self._wall_rtp_task: Optional[asyncio.Task] = None
        self._sync_correction_task: Optional[asyncio.Task] = None
        self._wall_stream_started_at_ms: Optional[int] = None
        self._wall_stream_loop_duration_s: Optional[float] = None  # current wall stream task (cancellable)

        # HTTP-served local-file wall playback state
        self._tile_duration_cache: dict = {}      # local_path -> duration_s
        self._sync_anchor_ms: Optional[int] = None    # wall_sync_at_ms of current play
        self._sync_duration_s: Optional[float] = None  # loop length of current tile file
        self._sync_active: bool = False               # gate for _sync_correction_loop
        self._wall_local_path: Optional[Path] = None  # currently-loaded local file

        # Local API server (so CMS can proxy NDI sources, preview, etc.)
        self._local_api = LocalApiServer()
        self._local_ip: str = _get_local_ip()

        if NDIReceiver is not None:
            self._ndi_receiver = NDIReceiver()
        if NDISender is not None:
            self._ndi_sender = NDISender()

        if ndi_available():
            log.info("[NDI] NDI support enabled")
        else:
            log.info("[NDI] NDI support not available — running without NDI")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=30)
        return self._http

    @property
    def _player_id(self) -> Optional[str]:
        return self.state.get("player_id")

    # ------------------------------------------------------------------
    # Enrollment
    # ------------------------------------------------------------------

    def _ensure_black_screen(self) -> None:
        """Play a tiny black PNG so mpv has a visible window for OSD text.

        Without content loaded, some mpv VO drivers won't render OSD at all.
        This creates a 1x1 black PNG in the cache dir and plays it paused.
        """
        black_path = CACHE_DIR / "_black.png"
        if not black_path.exists():
            try:
                # Minimal valid 1x1 black PNG (67 bytes)
                import struct, zlib
                def _png_chunk(chunk_type, data):
                    c = chunk_type + data
                    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
                sig = b"\x89PNG\r\n\x1a\n"
                ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                raw = zlib.compress(b"\x00\x00\x00\x00")
                idat = _png_chunk(b"IDAT", raw)
                iend = _png_chunk(b"IEND", b"")
                black_path.write_bytes(sig + ihdr + idat + iend)
            except Exception as e:
                log.warning(f"Could not create black PNG: {e}")
                return
        log.info("Loading black screen for OSD overlay...")
        self.mpv.play_file(str(black_path))
        # Pause immediately so it stays on screen as a static black frame
        try:
            self.mpv._mpv.pause = True
        except Exception:
            pass

    async def _request_enrollment(self) -> None:
        """POST /enroll/request → display code → poll for approval."""
        # Ensure mpv window is visible with a black background
        self._ensure_black_screen()
        await asyncio.sleep(0.5)  # give mpv a moment to open the window

        while self._running:
            try:
                log.info("Requesting enrollment code from CMS...")
                r = await self._http_client().post(
                    f"{self.cms_url}/api/v1/players/enroll/request"
                )
                r.raise_for_status()
                code = r.json()["code"]
                log.info(f"Enrollment code: {code}")
                self._show_enrollment_osd(code)
                await self._poll_enrollment(code)
                return  # approved — done
            except Exception as e:
                log.error(f"Enrollment request failed: {e}  — retrying in 10s")
                await asyncio.sleep(10)

    def _show_enrollment_osd(self, code: str) -> None:
        """Show enrollment code prominently via mpv OSD."""
        msg = (
            f"ENROLLMENT CODE\n\n"
            f"  {code}  \n\n"
            f"Enter this code in the CMS portal to approve this player."
        )
        self.mpv.show_osd(msg, duration_ms=9_000)

    async def _poll_enrollment(self, code: str) -> None:
        """Poll /enroll/{code}/status every 5 s until approved/expired."""
        url = f"{self.cms_url}/api/v1/players/enroll/{code}/status"
        while self._running:
            # Keep OSD visible while we wait
            self._show_enrollment_osd(code)
            await asyncio.sleep(5)
            try:
                r = await self._http_client().get(url)
                if r.status_code == 404:
                    # Code unknown — get a new one
                    log.warning("Enrollment code not found — requesting new code")
                    return  # outer loop will re-request
                r.raise_for_status()
                data = r.json()
                status = data.get("status", "")
                if status == "approved" and data.get("player_id"):
                    log.info(f"Approved!  player_id={data['player_id']}")
                    self.state["player_id"] = data["player_id"]
                    save_state(self.state)
                    self.mpv.clear_osd()  # clear enrollment overlay
                    await self._start_player_routines()
                    return
                elif status in ("expired", "rejected"):
                    log.warning(f"Enrollment {status} — requesting new code")
                    return  # outer loop re-requests
                # else still pending — continue polling
            except Exception as e:
                log.error(f"Enrollment poll error: {e}")
                # Don't exit — keep showing code and retrying

    # ------------------------------------------------------------------
    # Player routines (post-enrollment)
    # ------------------------------------------------------------------

    async def _start_player_routines(self) -> None:
        """
        Mirror of startPlayerRoutines() in main.js:
        1. Boot from offline_playlist.json instantly if available
        2. Fetch latest playlist from CMS in background
        3. Start 500 ms heartbeat loop
        """
        log.info("Starting player routines...")

        # 1. Boot from cache
        self.boot_cache_loaded = False
        if OFFLINE_PLAYLIST_PATH.exists():
            log.info("[BOOT] Loading cached playlist for instant playback...")
            try:
                offline_data = json.loads(OFFLINE_PLAYLIST_PATH.read_text())
                asyncio.create_task(self._process_and_download_playlist(offline_data))
                self.boot_cache_loaded = True
            except Exception as e:
                log.error(f"[BOOT] Failed to load cached playlist: {e}")

        # 2. Fetch latest in background
        asyncio.create_task(self._fetch_playlist())

        # 3. Heartbeat
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        # Send initial heartbeat immediately
        asyncio.create_task(self._send_heartbeat())

    # ------------------------------------------------------------------
    # Fetch playlist
    # ------------------------------------------------------------------

        # Stuck-player recovery watchdog (auto-restart if mpv hangs)
        try:
            asyncio.create_task(self._stuck_player_watchdog())
        except Exception:
            pass

    async def _fetch_playlist(self) -> None:
        """
        GET /players/{id}/assigned-playlist and process it.
        NOTE: must NOT set last_playlist_hash — only the heartbeat handler does.
        """
        try:
            log.info("Fetching assigned playlist from CMS...")
            r = await self._http_client().get(
                f"{self.cms_url}/api/v1/players/{self._player_id}/assigned-playlist"
            )
            r.raise_for_status()
            playlist_data = r.json()
            if not playlist_data:
                log.info("No playlist assigned to this player on the CMS.")
                return
            log.info("Playlist retrieved — processing downloads...")
            await self._process_and_download_playlist(playlist_data)
        except Exception as e:
            log.error(f"Failed to fetch playlist: {e}")
            if not self.boot_cache_loaded and OFFLINE_PLAYLIST_PATH.exists():
                log.info("[OFFLINE] Using cached playlist.")
                try:
                    offline_data = json.loads(OFFLINE_PLAYLIST_PATH.read_text())
                    await self._process_and_download_playlist(offline_data)
                except Exception as e2:
                    log.error(f"Failed to parse offline playlist: {e2}")

    # ------------------------------------------------------------------
    # Load single content item (load_content command)
    # ------------------------------------------------------------------

    async def _load_single_content(self, content_id: str) -> None:
        """Fetch metadata for a single content item and play it."""
        try:
            r = await self._http_client().get(
                f"{self.cms_url}/api/v1/content/{content_id}"
            )
            r.raise_for_status()
            meta = r.json()
            filename = meta.get("filename") or f"{content_id}.mp4"
            mock_playlist = {
                "items": [{
                    "content_id": content_id,
                    "content": {"filename": filename},
                    "duration": 0,
                }]
            }
        except Exception as e:
            log.warning(f"[CONTENT] Metadata fetch failed for {content_id}: {e}")
            mock_playlist = {
                "items": [{"content_id": content_id, "duration": 0}]
            }
        await self._process_and_download_playlist(mock_playlist)

    # ------------------------------------------------------------------
    # Process and download playlist  (core logic)
    # ------------------------------------------------------------------

    async def _process_and_download_playlist(self, playlist_data: dict) -> None:
        """
        Mirror of processAndDownloadPlaylist() in main.js.

        1. Save offline_playlist.json immediately.
        2. Build local path list.
        3. Check 50 GB cache limit.
        4. Download missing files sequentially; kick off playback after first file ready.
        5. If all cached: start playback immediately.
        6. LRU garbage-collect old files.
        """
        items = playlist_data.get("items") or []
        if not items:
            log.info("No items in playlist.")
            return

        # Save for offline boot
        try:
            OFFLINE_PLAYLIST_PATH.write_text(json.dumps(playlist_data))
        except Exception as e:
            log.error(f"Failed to save offline playlist: {e}")

        # Build per-item records
        to_play: list = []       # final ordered list of {path, duration, filename}
        to_download: list = []   # {remote_url, local_path, filename, index}

        for item in items:
            content_id = item.get("content_id")
            if not content_id:
                continue

            duration = item.get("duration", 0) or 0
            content_type = item.get("content", {}).get("type", "")

            # Templates are rendered HTML — display via browser, no download needed
            if content_type == "template":
                render_url = f"{self.cms_url}/api/v1/content/{content_id}/stream"
                display_name = item.get("content", {}).get("name") or f"template_{content_id}"
                to_play.append({
                    "type": "template",
                    "url": render_url,
                    "duration": duration,
                    "filename": display_name,
                })
                continue

            remote_url = f"{self.cms_url}/api/v1/content/{content_id}/stream"

            # Determine filename / extension
            filename = None
            if item.get("content") and item["content"].get("filename"):
                filename = item["content"]["filename"]
            if not filename:
                filename = f"{content_id}.mp4"

            local_path = CACHE_DIR / filename

            to_play.append({
                "path": local_path,
                "duration": duration,
                "filename": filename,
            })

            if not local_path.exists():
                to_download.append({
                    "remote_url": remote_url,
                    "local_path": local_path,
                    "filename": filename,
                    "index": len(to_play) - 1,
                })

        # 50 GB cache limit check
        if to_download:
            cache_bytes = _cache_size_bytes()
            if cache_bytes >= CACHE_LIMIT_BYTES:
                log.error(
                    "Cache exceeds 50 GB limit — refusing new downloads."
                )
                return

        if not to_download:
            # All cached — start immediately
            log.info("All files cached — starting playback immediately.")
            await self._load_playlist(to_play)
            self._gc_cache(playlist_data)
            return

        # Progressive playback: download sequentially, start on first ready
        log.info(
            f"Need to download {len(to_download)} file(s).  "
            f"Will start playback after first file is ready."
        )
        initial_triggered = False

        for i, dl in enumerate(to_download):
            log.info(
                f"[DOWNLOADING {i+1}/{len(to_download)}] "
                f"{dl['filename']}  from  {dl['remote_url']}"
            )
            success = await self._download_file(
                dl["remote_url"], dl["local_path"]
            )
            if not success:
                log.error(f"Download failed for {dl['filename']} — skipping")
                await asyncio.sleep(2)

            # After first successful download, kick off playback with whatever
            # is available so far (only cached files will be included)
            first = to_play[0]
            first_ready = (first.get("type") == "template" or
                           (first.get("path") is not None and first["path"].exists()))
            if not initial_triggered and first_ready:
                log.info("First file ready — starting playback.")
                ready = [e for e in to_play if e.get("type") == "template" or
                         (e.get("path") is not None and e["path"].exists())]
                await self._load_playlist(ready)
                initial_triggered = True

        # All downloads attempted — reload playlist with all available files
        ready = [e for e in to_play if e.get("type") == "template" or
                 (e.get("path") is not None and e["path"].exists())]
        if ready:
            log.info(
                f"Downloads complete — updating playlist "
                f"({len(ready)}/{len(to_play)} files)."
            )
            await self._load_playlist(ready)

        self._gc_cache(playlist_data)

    async def _download_file(self, url: str, dest: Path) -> bool:
        """Download url → dest using .tmp rename pattern.  Returns True on success."""
        tmp = Path(str(dest) + ".tmp")
        try:
            async with self._http_client().stream("GET", url) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
            tmp.rename(dest)
            log.info(f"[SUCCESS] Downloaded {dest.name}")
            return True
        except Exception as e:
            log.error(f"[ERROR] Failed to download {dest.name}: {e}")
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return False

    # ------------------------------------------------------------------
    # Playlist playback engine
    # ------------------------------------------------------------------

    async def _load_playlist(self, items: list) -> None:
        """
        Replace the current playlist and start from item 0.
        items = list of {path, duration, filename}
        """
        self._cancel_duration_timer()
        # If we're switching from a native-loop single item to a multi-item
        # playlist, disable the loop-file flag before loading the first item.
        if len(items) != 1:
            try:
                self.mpv._mpv.loop_file = "no"
            except Exception:
                pass
        self._playlist_items = items
        self._current_index = -1
        if items:
            await self._play_index(0)

    async def _play_index(self, index: int) -> None:
        """Play the item at the given index."""
        self._cancel_duration_timer()
        self.mpv.clear_osd()

        if not self._playlist_items:
            return

        index = index % len(self._playlist_items)
        self._current_index = index
        item = self._playlist_items[index]

        # Templates are displayed via Chromium browser
        if item.get("type") == "template":
            await self._play_template(item)
            return

        path: Path = item["path"]
        duration: int = item.get("duration", 0) or 0
        filename: str = item.get("filename", path.name)

        if not path.exists():
            log.warning(f"File not found, skipping: {path}")
            await asyncio.sleep(0.5)
            await self._advance()
            return

        is_img = _is_image(filename)

        # For a single-item video playlist with no explicit duration, tell mpv
        # to loop natively — this avoids any black frame on restart because mpv
        # never actually unloads the file between loops.
        single_item = len(self._playlist_items) == 1
        use_loop = single_item and not is_img and duration == 0

        # When in wall playlist mode, apply the crop filter to every item
        vf = _build_crop_filter(self._wall_crop) if self._wall_crop else None

        log.info(
            f"Playing [{index+1}/{len(self._playlist_items)}] "
            f"{filename}  (duration={duration}, loop={use_loop}{', wall_crop' if vf else ''})"
        )
        self.mpv.play_file(str(path), loop=use_loop, vf=vf)

        if is_img:
            # Images: always use a timer (default 10 s if duration == 0)
            secs = duration if duration > 0 else DEFAULT_IMAGE_DURATION
            self._duration_timer = asyncio.create_task(
                self._duration_then_advance(secs)
            )
        elif duration > 0:
            # Video with explicit duration: use a timer too
            self._duration_timer = asyncio.create_task(
                self._duration_then_advance(duration)
            )
        # else: video with duration==0 — wait for mpv EOF callback (or loop natively)

    async def _play_template(self, item: dict) -> None:
        """Display a template in Chromium kiosk mode, then advance after duration."""
        url: str = item["url"]
        duration: int = item.get("duration", 0) or 0
        effective_duration = duration if duration > 0 else 30
        filename: str = item.get("filename", "template")

        log.info(
            f"Playing template [{self._current_index + 1}/{len(self._playlist_items)}] "
            f"{filename}  (duration={effective_duration}s)"
        )

        chromium = _find_chromium()
        if not chromium:
            log.error("No Chromium browser found — cannot display template. Skipping.")
            await asyncio.sleep(1)
            await self._advance()
            return

        # Pause mpv so it doesn't run audio/video underneath
        try:
            self.mpv._mpv.pause = True
        except Exception:
            pass

        try:
            self._chromium_proc = await asyncio.create_subprocess_exec(
                chromium,
                "--kiosk",
                "--noerrdialogs",
                "--disable-infobars",
                "--no-first-run",
                "--disable-translate",
                "--disable-features=TranslateUI",
                "--disable-notifications",          # block web push notifications
                "--no-default-browser-check",       # suppress "make default browser" popup
                "--disable-session-crashed-bubble", # suppress crash recovery dialog
                "--block-new-web-contents",         # prevent popup windows / new tabs
                "--disable-popup-blocking",         # let the page open what it needs (but --block-new-web-contents overrides for new windows)
                "--disable-background-networking",  # no background update checks
                "--disable-client-side-phishing-detection",
                "--disable-component-update",       # suppress Chrome update prompts
                f"--display={self.display}",
                url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as e:
            log.error(f"Failed to launch Chromium for template: {e}")
            await self._advance()
            return

        self._duration_timer = asyncio.create_task(
            self._duration_then_advance(effective_duration)
        )

    # ------------------------------------------------------------------
    # Video wall helpers
    # ------------------------------------------------------------------

    async def _play_wall_local(self, url: str, play_at_ms: Optional[int] = None, emit_audio: bool = False) -> None:
        """Wall stream — HTTP-served local-file playback.

        Pipeline:
          1. Download (or 304-cache-hit) the per-tile MP4 from server
          2. Probe duration once, cache it
          3. Compute expected_pts = (now - wall_sync_at_ms) % duration
             — same wall_sync_at_ms on every player + chrony-synced clocks
             = identical expected_pts to within ~6us across all 16 players
          4. mpv loadfile <local> with start=expected_pts, loop-file=inf,
             pause=yes
          5. Wait for first frame decoded AND wall_sync_at_ms wallclock arrival
          6. command_async unpause (returns in ~50us)
          7. Arm _sync_correction_loop (seek-based — works on local files)
        """
        log.info(f"[WALL LOCAL] {url}  play_at_ms={play_at_ms}")
        self._cancel_duration_timer()

        # 1) DOWNLOAD (off the asyncio loop) — Python 3.8 compat
        local_path = await asyncio.get_event_loop().run_in_executor(
            None, _download_tile_with_etag, url)
        if local_path is None or not local_path.exists():
            log.error("[WALL LOCAL] download/cache miss; aborting this push")
            return

        # 2) DURATION (cached per file)
        duration_s = self._tile_duration_cache.get(str(local_path))
        if duration_s is None:
            duration_s = await asyncio.get_event_loop().run_in_executor(
                None, _ffprobe_duration, local_path)
            if duration_s > 0:
                self._tile_duration_cache[str(local_path)] = duration_s
        if duration_s <= 0:
            log.warning(f"[WALL LOCAL] could not determine duration of {local_path.name}; using 0 (no loop sync)")
            duration_s = 0.0

        # 3) EXPECTED START PTS at the sync moment
        if play_at_ms:
            elapsed_ms = int(time.time() * 1000) - play_at_ms
            if duration_s > 0:
                expected_pts = (elapsed_ms / 1000.0) % duration_s
                if expected_pts < 0:
                    expected_pts += duration_s
            else:
                expected_pts = 0.0
        else:
            expected_pts = 0.0

        # Apply persistent props for local playback.
        # video-sync=desync: do NOT lock playback rate to display refresh rate.
        #   display-resample causes 0.1-0.5% rate variance per physical panel
        #   (different displays = different actual refresh Hz), which shows up
        #   as Player-25 consistently 100-300ms ahead of others. desync uses
        #   the system clock (chrony-synced across all 16) so every player
        #   advances at the same true rate, eliminating per-display drift.
        # display-fps-override locks the assumed refresh to a clean 60.0 Hz
        #   for the display-resample fallback case.
        local_props = {
            "video-sync":  "desync",
            "interpolation": False,
            "framedrop":   "no",
            "hwdec":       "auto-safe",
            "audio":       ("auto" if emit_audio else "no"),
            "keep-open":   "always",
            "loop-file":   "inf",
            "cache":       "no",
            "display-fps-override": 60.0,
        }
        for _k, _v in local_props.items():
            try:
                self.mpv._mpv[_k] = _v
            except Exception:
                pass

        try:
            # Compute the target unpause wallclock + expected_pts at THAT moment,
            # so all players land on the same PTS at the same wallclock.
            if play_at_ms and duration_s > 0:
                now_ms = int(time.time() * 1000)
                target_unpause_ms = play_at_ms if play_at_ms > now_ms + 200 else now_ms + 1500
                expected_pts_at_unpause = ((target_unpause_ms - play_at_ms) / 1000.0) % duration_s
                if expected_pts_at_unpause < 0:
                    expected_pts_at_unpause += duration_s
            else:
                target_unpause_ms = int(time.time() * 1000) + 1500
                expected_pts_at_unpause = 0.0

            # SYNC pause via dict-set (blocks until applied) BEFORE loadfile
            try:
                self.mpv._mpv["pause"] = True
            except Exception:
                pass

            t_load_ms = int(time.time() * 1000)
            log.info(f"[WALL LOCAL] play_file start_paused {local_path.name} "
                     f"(duration={duration_s:.3f}s, unpause in {target_unpause_ms - t_load_ms}ms)")
            # Use the wrapper's play_file with start_paused=True — the proven
            # path that handles loop_file/vf/_deliberate_load correctly across
            # python-mpv binding versions. play_file does NOT call cmd_stop;
            # combined with keep-open=always, this gives a clean transition
            # from the previous frame to the new one with no blank flash.
            try:
                self.mpv.play_file(str(local_path), loop=True, vf=None,
                                   start_paused=True)
            except Exception as e:
                log.error(f"[WALL LOCAL] play_file failed: {e}")
                return

            # 5a) BARRIER — wait for first frame decoded (paused at PTS 0)
            ff_deadline_ms = target_unpause_ms - 100
            ff_ready_ms = None
            while int(time.time() * 1000) < ff_deadline_ms:
                pt = getattr(self.mpv._mpv, "playback_time", None)
                if pt is not None and pt >= 0:
                    ff_ready_ms = int(time.time() * 1000)
                    break
                await asyncio.sleep(0.01)
            if ff_ready_ms is None:
                log.warning(f"[WALL LOCAL] first frame NOT ready by deadline ({int(time.time()*1000) - t_load_ms}ms)")
            else:
                log.info(f"[WALL LOCAL] first frame ready @{ff_ready_ms} (load {ff_ready_ms - t_load_ms}ms)")

            # 5b) Defensive re-pause via dict-set (sync). At this point file
            # is loaded, paused, and at PTS=~0.
            try:
                self.mpv._mpv["pause"] = True
            except Exception:
                pass

            # 5c) WAIT for unpause wallclock
            wait_to_play = target_unpause_ms - int(time.time() * 1000)
            if wait_to_play > 0:
                await asyncio.sleep(wait_to_play / 1000.0)

            pre_unpause_pts = getattr(self.mpv._mpv, "playback_time", None)
            # 6) UNPAUSE — sync via dict-set so we KNOW it took effect
            try:
                self.mpv._mpv["pause"] = False
            except Exception:
                pass
            t_unpaused_ms = int(time.time() * 1000)
            log.info(f"[WALL LOCAL] UNPAUSED @{t_unpaused_ms} target={target_unpause_ms} "
                     f"drift={t_unpaused_ms - target_unpause_ms:+d}ms "
                     f"pre_unpause_pts={pre_unpause_pts}")

            # 7) Arm the sync-correction loop with this play's anchor + duration
            self._sync_anchor_ms = play_at_ms
            self._sync_duration_s = duration_s
            self._wall_local_path = local_path
            self._sync_active = (play_at_ms is not None and duration_s > 0)
            if self._sync_correction_task is None or self._sync_correction_task.done():
                self._sync_correction_task = asyncio.create_task(self._sync_correction_loop())
                log.info("[WALL LOCAL] seek-based sync correction loop armed")

            # Idle-watchdog (same as RTSP path — auto-recover if mpv crashed)
            await asyncio.sleep(5)
            while True:
                await asyncio.sleep(2)
                try:
                    if self.mpv._mpv["core-idle"]:
                        log.warning("[WALL LOCAL] mpv idle — clearing key for auto-recovery")
                        self._ndi_active_key = None
                        try:
                            self.mpv._mpv["keep-open"] = "always"
                        except Exception:
                            pass
                        return
                except Exception:
                    return
        except asyncio.CancelledError:
            log.info("[WALL LOCAL] task cancelled (new push)")
            self._sync_active = False
            return

    async def _play_wall_rtp(self, rtp_url: str, crop: Optional[dict],
                              play_at_ms: Optional[int] = None, emit_audio: bool = False) -> None:
        """Video wall: open a pre-cropped RTSP tile stream from the server.

        The server does all the cropping — each player gets its own tile at
        native resolution via RTSP/TCP. No client-side filter needed.

        If play_at_ms is given, the stream is opened with pause=True and
        unpaused exactly at that wallclock moment so all 16 players display
        the same first frame within microseconds of each other (chrony-synced).

        Monitors mpv — if it goes idle (server restarted ffmpeg for content
        switch), clears the dedup key so the next heartbeat re-opens.
        """
        log.info(f"[WALL STREAM] Opening {rtp_url}  play_at_ms={play_at_ms}")
        self._cancel_duration_timer()
        # RTSP streams are NOT seekable. The seek-based sync correction loop
        # only makes sense for HTTP-served local MP4s (the frame-accurate
        # path). Disarm any leftover sync state from a previous local-file
        # push so the correction loop doesn't try to seek on this stream.
        self._sync_active = False
        self._sync_anchor_ms = None
        self._sync_duration_s = None
        # NOTE: deliberately NOT calling self.mpv.cmd_stop() here. cmd_stop
        # immediately blanks the screen; we instead let the old frame stay
        # visible during the Phase 1 wait, then mpv's loadfile (called by
        # play_file) does an old-frame -> new-first-frame transition with
        # no idle/blank state in between. Combined with keep-open=always
        # this gives a flash-free Push Live.

        try:
            # No leading 0.3s sleep — wastes time before Phase 1 wait. Apply
            # the persistent RTSP tuning props directly.
            #
            # Two design decisions worth calling out (we tried both other
            # ways before, the trade-offs below are why these are correct):
            #
            # video-sync=desync (NOT display-resample)
            #   display-resample locks playback rate to the local panel's
            #   actual refresh, which differs 0.1-0.5% between physical
            #   monitors. Two players watching the same byte stream would
            #   drift relative to each other. desync uses the system clock,
            #   which chrony keeps within ~µs across the fleet — so all
            #   players advance at the same true rate by construction.
            #   This is the SAME setting the local-file path uses for
            #   exactly the same reason.
            #
            # Tight buffers (cache-secs / demuxer-readahead 0.2 each)
            #   was 0.5 each, i.e. up to 1 s of "look-ahead" before a frame
            #   showed. On a tightly-controlled web-app wall (WebSocket
            #   button presses) this turned 60 ms of real work into a 1+ s
            #   visible delay. 0.2 s is enough to absorb LAN jitter without
            #   ballooning the latency budget.
            #
            # Low-latency demuxer extras
            #   - probesize/analyzeduration cut from 1 s to 0.1 s: open
            #     a fresh RTSP session in <100 ms instead of a full second.
            #   - demuxer-lavf-o=fflags=+nobuffer: don't bunch packets at
            #     the demuxer.
            #   - video-latency-hacks=yes: skip mpv's lip-sync verification
            #     pass; harmless on signage (audio=no for most players).
            rtsp_props = {
                "rtsp-transport":              "tcp",
                "cache":                       "yes",
                "cache-secs":                  0.2,                       # was 0.5
                "demuxer-readahead-secs":      0.2,                       # was 0.5
                "demuxer-lavf-probesize":      32768,                     # was 524288
                "demuxer-lavf-analyzeduration": 100000,                   # was 1000000 (1 s → 0.1 s)
                "demuxer-lavf-o":              "fflags=+nobuffer",
                "video-sync":                  "desync",                  # was display-resample
                "video-latency-hacks":         True,
                "interpolation":               False,
                "framedrop":                   "no",
                "hwdec":                       "auto-safe",
                "audio":                       ("auto" if emit_audio else "no"),
                "audio-buffer":                0,
                "keep-open":                   "always",
                # Force a clean 60 Hz refresh assumption so desync's fall-
                # back math is identical across players with mismatched
                # monitors.
                "display-fps-override":        60.0,
            }
            for _k, _v in rtsp_props.items():
                try:
                    self.mpv._mpv[_k] = _v
                except Exception:
                    pass

            # === 3-phase synchronized open + first-frame-ready barrier ===
            # Critical fix: between Phase 2 (loadfile) and Phase 3 (unpause)
            # we BLOCK until mpv has actually decoded a first frame. Without
            # this, a slow-loading player would unpause mid-load and start
            # playing from the live stream position when load completed —
            # which differs per player by hundreds of ms. With the barrier,
            # every player guarantees "first frame decoded, paused" before
            # the shared unpause tick, so the unpause flips ALL players from
            # the same paused frame to the same playing frame.
            OPEN_BEFORE_PLAY_MS = 800
            if play_at_ms:
                # Phase 1 — old frame stays on screen during this wait
                now_ms = int(time.time() * 1000)
                open_at_ms = play_at_ms - OPEN_BEFORE_PLAY_MS
                wait_to_open = open_at_ms - now_ms
                if wait_to_open > 0:
                    log.info(f"[WALL STREAM] Phase 1: hold old frame {wait_to_open}ms (until shared OPEN)")
                    await asyncio.sleep(wait_to_open / 1000.0)

                # Phase 2 — set pause (async, non-blocking), loadfile, BLOCK
                # until first frame decoded.
                try:
                    self.mpv._mpv.command_async("set_property", "pause", True)
                except Exception:
                    pass
                t_p2_start_ms = int(time.time() * 1000)
                log.info(f"[WALL STREAM] Phase 2: loadfile @{t_p2_start_ms}")
                self.mpv.play_file(rtp_url, loop=False, vf=None)

                # First-frame-ready barrier — poll until mpv reports a real
                # playback-time. Time out at play_at_ms - 100ms so we always
                # leave a small unpause-prep window even if loading dragged.
                # Defensive re-pause via command_async if mpv slipped into
                # playing state (would happen if our initial pause=True
                # property set was processed AFTER the loadfile started).
                ff_deadline_ms = play_at_ms - 100
                ff_ready_ms = None
                while int(time.time() * 1000) < ff_deadline_ms:
                    try:
                        pt = self.mpv._mpv["playback-time"]
                        if pt is not None and pt >= 0:
                            # File is loaded — defensively re-pause via
                            # command_async (non-blocking) and break.
                            try:
                                self.mpv._mpv.command_async("set_property", "pause", True)
                            except Exception:
                                pass
                            ff_ready_ms = int(time.time() * 1000)
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.02)
                if ff_ready_ms is None:
                    log.warning("[WALL STREAM] Phase 2: first frame NOT ready by deadline")
                else:
                    log.info(f"[WALL STREAM] Phase 2: first frame ready @{ff_ready_ms} (load {ff_ready_ms - t_p2_start_ms}ms)")

                # Phase 3 — wait, then unpause via command_async (non-blocking).
                # The earlier sync version of this set used __setitem__ which
                # waits for mpv to ack the property change; on a busy mpv that
                # could block the asyncio coroutine for up to a second,
                # producing a visible single-player offset on the wall. With
                # command_async, the python side returns in ~50us; mpv applies
                # pause=no on its next IPC tick (typically <5ms).
                now_ms = int(time.time() * 1000)
                wait_to_play = play_at_ms - now_ms
                if wait_to_play > 0:
                    log.info(f"[WALL STREAM] Phase 3: unpause in {wait_to_play}ms")
                    await asyncio.sleep(wait_to_play / 1000.0)
                try:
                    self.mpv._mpv.command_async("set_property", "pause", False)
                except Exception:
                    pass
                t_unpaused_ms = int(time.time() * 1000)
                log.info(f"[WALL STREAM] UNPAUSED @{t_unpaused_ms} target={play_at_ms} drift={t_unpaused_ms - play_at_ms}ms")
                # Arm the RTSP-specific drift correction loop. SPEED-NUDGE
                # only — RTSP isn't seekable so micro-seek / hard-rescue
                # don't apply, but per-player decoder rate variance still
                # accumulates into visible drift over minutes if nothing
                # corrects it. _rtsp_sync_loop enforces the invariant
                # "playback_time advances 1:1 with wallclock" with sub-JND
                # ±1.5 % speed nudges. Cancelled by _cancel_wall_rtp_task.
                if self._sync_correction_task is None or self._sync_correction_task.done():
                    self._sync_correction_task = asyncio.create_task(self._rtsp_sync_loop())
                    log.info("[WALL STREAM] RTSP drift-correction loop armed")
            else:
                # No sync tick — immediate transition (still no cmd_stop)
                self.mpv.play_file(rtp_url, loop=False, vf=None)

            # Monitor for stream death
            await asyncio.sleep(5)
            while True:
                await asyncio.sleep(2)
                try:
                    if self.mpv._mpv["core-idle"]:
                        log.warning("[WALL STREAM] Stream died — clearing key for auto-recovery")
                        self._ndi_active_key = None
                        try:
                            self.mpv._mpv["keep-open"] = "always"
                        except Exception:
                            pass
                        return
                except Exception:
                    return
        except asyncio.CancelledError:
            log.info("[WALL STREAM] Task cancelled (switching content)")
            try:
                self.mpv._mpv["keep-open"] = "always"
            except Exception:
                pass
            return

    async def _rtsp_sync_loop(self) -> None:
        """Speed-nudge drift correction for RTSP streams — NO seeks.

        RTSP is not seekable, so the seek-based _sync_correction_loop
        used for local files would 404 on every cycle. Instead we keep
        the wall in lockstep by enforcing the invariant:

            playback_time advances 1:1 with wallclock.

        Phase-3 unpause aligns every player to the same shared (wallclock,
        playback_time) anchor (within ~µs courtesy of chrony). After that,
        per-decoder rate variance — typically ±0.1 % — slowly walks the
        wall apart. We measure drift = elapsed_wallclock - elapsed_pt
        every 2 s and apply a proportional speed correction clamped at
        ±1.5 %. That's well below motion JND (~3 %) so the viewer never
        sees the nudge, but cumulative drift converges back to zero.

        Cancelled by _cancel_wall_rtp_task on Push Live or stop_wall.
        """
        SETTLE_S       = 3.0       # let playback stabilise before anchoring
        PERIOD_S       = 2.0
        DEAD_BAND_MS   = 30        # ±1 frame @ 30fps → ignore
        MAX_ADJ        = 0.015     # max ±1.5 % speed change
        GAIN_PER_MS    = 0.00005

        log.info("[RTSP-SYNC] settling before anchor")
        try:
            await asyncio.sleep(SETTLE_S)

            # Acquire a stable anchor — playback_time must be ≥ 0 and
            # mpv must actually be playing (core-idle == False).
            anchor_wc_ms = None
            anchor_pt = None
            for _ in range(10):
                try:
                    if self.mpv._mpv["core-idle"]:
                        await asyncio.sleep(0.5)
                        continue
                    pt = self.mpv._mpv["playback-time"]
                    if pt is not None and pt >= 0:
                        anchor_wc_ms = int(time.time() * 1000)
                        anchor_pt = float(pt)
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)

            if anchor_wc_ms is None:
                log.warning("[RTSP-SYNC] could not anchor (mpv never went active) — giving up")
                return
            log.info(f"[RTSP-SYNC] anchored: wc={anchor_wc_ms} pt={anchor_pt:.3f}")

            while True:
                await asyncio.sleep(PERIOD_S)
                try:
                    pt = self.mpv._mpv["playback-time"]
                    if pt is None or pt < 0:
                        continue
                    if self.mpv._mpv["core-idle"]:
                        continue
                except Exception:
                    continue

                now_wc_ms = int(time.time() * 1000)
                elapsed_wc_ms = now_wc_ms - anchor_wc_ms
                elapsed_pt_ms = (float(pt) - anchor_pt) * 1000.0
                drift_ms = elapsed_wc_ms - elapsed_pt_ms   # +ve: we're behind

                if abs(drift_ms) < DEAD_BAND_MS:
                    try:
                        self.mpv._mpv.command_async("set_property", "speed", 1.0)
                    except Exception:
                        pass
                    continue

                adj = max(-MAX_ADJ, min(MAX_ADJ, drift_ms * GAIN_PER_MS))
                new_speed = 1.0 + adj
                try:
                    self.mpv._mpv.command_async("set_property", "speed", new_speed)
                    log.info(f"[RTSP-SYNC] drift={drift_ms:+.0f}ms → speed={new_speed:.4f}")
                except Exception:
                    pass

        except asyncio.CancelledError:
            try:
                self.mpv._mpv.command_async("set_property", "speed", 1.0)
            except Exception:
                pass
            log.info("[RTSP-SYNC] cancelled")
            return

    async def _stuck_player_watchdog(self) -> None:
        """Recovery for genuinely-stuck mpv. CONSERVATIVE — was firing on
        normal load transitions and looking like the "screenshots of video"
        symptom (each rescue caused a brief re-load that flashed first
        frames).

        Now only fires when ALL of these hold:
          - sync has been active for at least 30 s (warm-up window)
          - playback_time has not advanced for 15 s
          - mpv reports core-idle == False (it thinks it's playing — so a
            stuck decode is real, not just paused or at EOF)
          - at least 60 s since the previous rescue (backoff)
        """
        WARMUP_S = 30.0
        STUCK_WINDOW_S = 15.0
        BACKOFF_S = 60.0
        sync_active_since = 0.0
        last_pt = None
        last_pt_ts = 0.0
        last_rescue_ts = 0.0
        try:
            while True:
                await asyncio.sleep(5.0)
                if not self._sync_active:
                    sync_active_since = 0.0
                    last_pt = None
                    continue
                now_s = time.time()
                if sync_active_since == 0.0:
                    sync_active_since = now_s
                    continue
                if now_s - sync_active_since < WARMUP_S:
                    last_pt = None
                    continue

                pt = getattr(self.mpv._mpv, "playback_time", None)
                try:
                    core_idle = bool(self.mpv._mpv["core-idle"])
                except Exception:
                    core_idle = True   # if we can't tell, assume idle (don't rescue)

                # If mpv says it's idle (paused, EOF, etc.) we are not stuck —
                # we just shouldn't be playing. Reset baseline.
                if core_idle:
                    last_pt = None
                    continue

                if pt is None or pt < 0:
                    last_pt = None
                    continue

                if last_pt is None:
                    last_pt = pt
                    last_pt_ts = now_s
                    continue
                if abs(pt - last_pt) >= 0.25:
                    last_pt = pt
                    last_pt_ts = now_s
                    continue

                stuck_for = now_s - last_pt_ts
                if stuck_for < STUCK_WINDOW_S:
                    continue
                if now_s - last_rescue_ts < BACKOFF_S:
                    continue

                log.warning(f"[WATCHDOG] genuinely stuck: pt={pt:.3f}s for "
                            f"{stuck_for:.1f}s, core-idle=False — re-arming once "
                            f"(next attempt allowed in {BACKOFF_S}s)")
                try:
                    self.mpv._mpv["pause"] = False
                except Exception:
                    pass
                self._ndi_active_key = None
                self._sync_active = False
                last_rescue_ts = now_s
                last_pt = None
                sync_active_since = 0.0
        except asyncio.CancelledError:
            return

    async def _sync_correction_loop(self) -> None:
        """Soft-only correction. Local files keep mpv playing at real-time
        once synchronized-unpaused, so cross-player drift is bounded by
        per-player decoder jitter — small, gradual. Speed nudge fixes it
        invisibly. NO hard seek (kept causing visible re-snap each cycle)."""
        SYNC_PERIOD_S = 1.0    # check every 1s — keeps drift tighter
        DEAD_BAND_MS = 20      # ±0.6 frame @30fps — image content shows even tiny drift
        MICRO_SEEK_MS = 100    # 20-100ms: speed nudge; 100-500ms: micro seek (frame-accurate snap)
        SPEED_GAIN_S = 1.0
        SPEED_CLAMP = 0.10     # max ±10% speed change
        HARD_THRESHOLD_MS = 500  # > 500ms: hard rescue (full re-arm)
        log.info("[SYNC] soft correction loop started (no hard seek)")
        try:
            await asyncio.sleep(2.0)
            while True:
                await asyncio.sleep(SYNC_PERIOD_S)
                if not self._sync_active or not self._sync_anchor_ms or not self._sync_duration_s:
                    continue
                D = float(self._sync_duration_s)
                if D <= 0:
                    continue
                actual_pts = getattr(self.mpv._mpv, "playback_time", None)
                if actual_pts is None or actual_pts < 0:
                    continue

                now_ms = int(time.time() * 1000)
                elapsed_s = (now_ms - self._sync_anchor_ms) / 1000.0
                expected_pts = elapsed_s % D
                if expected_pts < 0:
                    expected_pts += D

                drift_s = expected_pts - actual_pts
                # Wrap modulo loop length so end-of-loop != huge drift
                if drift_s > D / 2:
                    drift_s -= D
                if drift_s < -D / 2:
                    drift_s += D
                drift_ms = drift_s * 1000.0

                if abs(drift_ms) <= DEAD_BAND_MS:
                    try:
                        self.mpv._mpv.command_async("set_property", "speed", 1.0)
                    except Exception:
                        pass
                    continue

                # Hard rescue: drift > 500ms — snap to expected via mpv.seek()
                if abs(drift_ms) > HARD_THRESHOLD_MS:
                    log.warning(f"[SYNC] HARD RESCUE expected={expected_pts:.4f}s "
                                f"actual={actual_pts:.4f}s drift={drift_ms:+.0f}ms — "
                                f"seeking to catch up")
                    try:
                        self.mpv._mpv.seek(expected_pts, "absolute")
                        self.mpv._mpv.command_async("set_property", "speed", 1.0)
                    except Exception as e:
                        log.warning(f"[SYNC] hard rescue failed: {e}")
                    continue

                # Micro seek: drift 100-500ms — frame-accurate snap (much faster
                # than waiting for soft speed to converge). For static-image
                # content (PPT slides), even 100ms drift is visible as different
                # players being on different images at the same wallclock.
                if abs(drift_ms) > MICRO_SEEK_MS:
                    log.info(f"[SYNC] MICRO-SEEK expected={expected_pts:.4f}s "
                             f"actual={actual_pts:.4f}s drift={drift_ms:+.0f}ms")
                    try:
                        self.mpv._mpv.seek(expected_pts, "absolute")
                        self.mpv._mpv.command_async("set_property", "speed", 1.0)
                    except Exception as e:
                        log.warning(f"[SYNC] micro seek failed: {e}")
                    continue

                # Soft speed nudge for normal small drifts
                ratio = 1.0 + drift_s / SPEED_GAIN_S
                if ratio < 1.0 - SPEED_CLAMP:
                    ratio = 1.0 - SPEED_CLAMP
                if ratio > 1.0 + SPEED_CLAMP:
                    ratio = 1.0 + SPEED_CLAMP
                log.info(f"[SYNC] soft expected={expected_pts:.4f}s actual={actual_pts:.4f}s "
                         f"drift={drift_ms:+.0f}ms speed={ratio:.4f}")
                try:
                    self.mpv._mpv.command_async("set_property", "speed", ratio)
                except Exception:
                    pass
        except asyncio.CancelledError:
            try:
                self.mpv._mpv.command_async("set_property", "speed", 1.0)
            except Exception:
                pass
            log.info("[SYNC] correction loop cancelled, speed reset")
            return

    async def _duration_then_advance(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
            await self._advance()
        except asyncio.CancelledError:
            pass

    async def _advance(self) -> None:
        """Move to the next item (wraps around)."""
        if not self._playlist_items:
            return
        next_index = (self._current_index + 1) % len(self._playlist_items)
        await self._play_index(next_index)

    def _cancel_wall_rtp_task(self) -> None:
        """Cancel any running _play_wall_rtp task and the soft-sync loop."""
        if self._wall_rtp_task and not self._wall_rtp_task.done():
            self._wall_rtp_task.cancel()
            log.debug("[WALL RTP] Cancelled previous wall task")
        self._wall_rtp_task = None
        if self._sync_correction_task and not self._sync_correction_task.done():
            self._sync_correction_task.cancel()
            log.debug("[WALL RTP] Cancelled previous sync-correction task")
        self._sync_correction_task = None

    def _cancel_duration_timer(self) -> None:
        if self._duration_timer and not self._duration_timer.done():
            self._duration_timer.cancel()
        self._duration_timer = None
        # Kill any running Chromium browser (template display)
        if self._chromium_proc is not None:
            try:
                self._chromium_proc.terminate()
            except Exception:
                pass
            self._chromium_proc = None

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Send a heartbeat every 500 ms."""
        while self._running:
            await asyncio.sleep(0.5)
            await self._send_heartbeat()

    async def _send_heartbeat(self) -> None:
        if not self._player_id:
            return
        try:
            heartbeat_payload = {
                "status": "online",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "ndi_available": ndi_available(),
                "local_ip": self._local_ip,
                "api_port": LOCAL_API_PORT,
                "player_version": PLAYER_VERSION,
                "uptime_seconds": int(time.time() - self._start_time),
            }

            # Playback state
            if self._playlist_items and 0 <= self._current_index < len(self._playlist_items):
                item = self._playlist_items[self._current_index]
                heartbeat_payload["current_content_name"] = item.get("filename", "")
                heartbeat_payload["playlist_index"] = self._current_index
                heartbeat_payload["playlist_total"] = len(self._playlist_items)

            # System metrics (psutil — optional)
            if _psutil is not None:
                try:
                    heartbeat_payload["cpu_percent"] = round(_psutil.cpu_percent(interval=None), 1)
                    heartbeat_payload["memory_percent"] = round(_psutil.virtual_memory().percent, 1)
                    heartbeat_payload["disk_percent"] = round(_psutil.disk_usage("/").percent, 1)
                except Exception:
                    pass
                # CPU temperature — try common Linux thermal sensor keys
                try:
                    temps = _psutil.sensors_temperatures()
                    for _key in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "cpu-thermal"):
                        if _key in temps and temps[_key]:
                            heartbeat_payload["cpu_temp"] = round(temps[_key][0].current, 1)
                            break
                except Exception:
                    pass

            # Include NDI broadcast info if active
            if self._ndi_sender and self._ndi_sender.active:
                heartbeat_payload["ndi_broadcast_active"] = True
                heartbeat_payload["ndi_broadcast_name"] = self._ndi_sender.source_name
            if self._ndi_receiver and self._ndi_receiver.active:
                heartbeat_payload["ndi_receive_active"] = True
                heartbeat_payload["ndi_receive_source"] = self._ndi_receiver.source_name

            r = await self._http_client().post(
                f"{self.cms_url}/api/v1/players/{self._player_id}/heartbeat",
                json=heartbeat_payload,
            )

            if r.status_code == 404:
                log.warning(
                    "[CMS DELETION] This player was removed — clearing state."
                )
                self.state.pop("player_id", None)
                clear_state()
                self._cancel_duration_timer()
                self.mpv.cmd_stop()
                # Re-enroll
                asyncio.create_task(self._request_enrollment())
                return

            r.raise_for_status()
            data = r.json()
            await self._handle_heartbeat_command(data)

        except httpx.HTTPStatusError:
            pass  # already handled 404 above; other errors logged below
        except Exception as e:
            log.error(f"Heartbeat failed: {e}")

    async def _handle_heartbeat_command(self, data: dict) -> None:
        """Dispatch commands received in the heartbeat response."""
        cmd = data.get("command")
        if not cmd or cmd == "none":
            return

        log.info(f"[HEARTBEAT COMMAND] {cmd}")

        if cmd == "load_playlist":
            incoming_hash = data.get("playlist_hash") or data.get("playlist_id")
            if incoming_hash and incoming_hash == self.last_playlist_hash:
                log.info("Playlist unchanged (hash match) — skipping.")
            else:
                self.last_playlist_hash = incoming_hash or None
                self.last_content_id = None
                self._ndi_active_key = None  # exit wall mode
                self._wall_crop = None
                self._cancel_wall_rtp_task()
                asyncio.create_task(self._fetch_playlist())

        elif cmd == "stop_wall":
            # Server tells us to exit wall mode — stop stream and revert to playlist
            log.info("[WALL] Received stop_wall — exiting wall mode")
            self._ndi_active_key = None
            self._wall_crop = None
            self._cancel_wall_rtp_task()
            self.mpv.cmd_stop()
            # Restore keep-open for normal playlist playback
            try:
                self.mpv._mpv["keep-open"] = "always"
            except Exception:
                pass
            # Resume individual playlist if we have one
            if self._playlist_items:
                asyncio.create_task(self._play_index(self._current_index))

        elif cmd == "load_content":
            content_id = data.get("content_id")
            if not content_id:
                return
            if content_id == self.last_content_id:
                log.info("Same content already loaded — skipping.")
            else:
                self.last_content_id = content_id
                self.last_playlist_hash = None
                self._ndi_active_key = None  # exit wall mode
                self._cancel_wall_rtp_task()
                log.info(f"Loading direct content: {content_id}")
                asyncio.create_task(self._load_single_content(content_id))

        elif cmd == "play":
            self.mpv.cmd_play()

        elif cmd == "pause":
            self.mpv.cmd_pause()

        elif cmd == "next":
            self._cancel_duration_timer()
            asyncio.create_task(self._advance())

        elif cmd == "previous":
            self._cancel_duration_timer()
            prev = (self._current_index - 1) % max(len(self._playlist_items), 1)
            asyncio.create_task(self._play_index(prev))

        elif cmd == "restart":
            self._cancel_duration_timer()
            asyncio.create_task(self._play_index(self._current_index))

        # -- NDI commands (mirror Windows player) --

        elif cmd == "show_ndi":
            source_name = data.get("source_name") or data.get("ndi_source_name")
            if source_name and self._ndi_receiver:
                log.info(f"[NDI] show_ndi: {source_name}")
                self._cancel_duration_timer()
                self.mpv.cmd_stop()  # stop regular playback
                self._ndi_receiver.start(source_name)
            else:
                log.warning("[NDI] show_ndi but source_name is empty or NDI unavailable")

        elif cmd == "hide_ndi":
            source_name = data.get("source_name") or data.get("ndi_source_name")
            if self._ndi_receiver:
                log.info(f"[NDI] hide_ndi: {source_name}")
                self._ndi_receiver.stop()
                self._ndi_active_key = None
                # Resume regular playback if we have a playlist
                if self._playlist_items:
                    asyncio.create_task(self._play_index(self._current_index))

        elif cmd == "show_ndi_wall":
            source_name = data.get("source_name") or data.get("ndi_source_name")
            crop = data.get("wall_crop")
            if source_name and self._ndi_receiver:
                wall_key = f"ndi_{source_name}_{json.dumps(crop)}"
                if wall_key == self._ndi_active_key:
                    log.info("[NDI WALL] Same NDI wall already active — skip")
                else:
                    self._ndi_active_key = wall_key
                    log.info(f"[NDI WALL] Starting: {source_name} crop={crop}")
                    self._cancel_duration_timer()
                    self.mpv.cmd_stop()
                    ndi_crop = None
                    if crop:
                        ndi_crop = {
                            "x": crop.get("x", 0),
                            "y": crop.get("y", 0),
                            "w": crop.get("w", 1),
                            "h": crop.get("h", 1),
                        }
                    self._ndi_receiver.start(source_name, crop=ndi_crop)

        elif cmd == "load_wall_rtp":
            # Wall stream — pre-cropped RTSP tile from server.
            #
            # Dedup includes play_at_ms so a fresh Push Live (server-issued
            # new sync tick) re-arms the 3-phase open even if the URL is
            # unchanged. Combined with keep-open=always and the removal of
            # cmd_stop in _play_wall_rtp, mpv's loadfile-replace keeps the
            # previous frame visible until the new first frame is ready —
            # no visible blank at any point in the cycle.
            rtp_url = data.get("rtp_url")
            emit_audio = bool(data.get("emit_audio", False))
            if not rtp_url:
                return
            # Capture soft-sync reference clock BEFORE the dedup check so it
            # refreshes on every load_wall_rtp heartbeat, not just on the very
            # first one. The first heartbeat after a push may have started_at=0
            # if it raced the server's get_stream_clock() spawn-wait; later
            # heartbeats carry the real value, but the dedup early-return would
            # otherwise prevent the player from ever seeing them.
            _started = data.get("wall_stream_started_at_ms")
            _loop = data.get("wall_stream_loop_duration_s")
            if _started:
                self._wall_stream_started_at_ms = int(_started)
            if _loop:
                self._wall_stream_loop_duration_s = float(_loop)
            play_at_ms = data.get("play_at_ms")
            wall_key = f"rtp_{rtp_url}_{play_at_ms or 0}"
            if wall_key == self._ndi_active_key:
                try:
                    if not self.mpv._mpv["core-idle"]:
                        # Already running this exact (URL, sync_tick) combo —
                        # heartbeat is just persistence-firing, no work to do.
                        return
                    log.info("[WALL STREAM] Same key but mpv idle — retrying")
                except Exception:
                    return
            self._cancel_wall_rtp_task()
            self._ndi_active_key = wall_key
            log.info(f"[WALL STREAM] {rtp_url}  play_at_ms={play_at_ms}  "
                     f"started_at={self._wall_stream_started_at_ms}  "
                     f"loop={self._wall_stream_loop_duration_s}")
            # HTTP URL -> new local-file playback path (seek + speed work)
            # rtsp:// URL -> legacy live-stream path (kept for rollback)
            if rtp_url.startswith("http://") or rtp_url.startswith("https://"):
                self._wall_rtp_task = asyncio.create_task(
                    self._play_wall_local(rtp_url, play_at_ms, emit_audio)
                )
            else:
                self._wall_rtp_task = asyncio.create_task(
                    self._play_wall_rtp(rtp_url, None, play_at_ms, emit_audio)
                )

        elif cmd == "restart_service":
            log.info("[RESTART] Restarting signage-player service...")
            subprocess.Popen(
                ["sudo", "systemctl", "restart", "signage-player"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return  # systemd will kill this process

        elif cmd == "start_ndi_broadcast":
            if self._ndi_sender:
                name = data.get("ndi_broadcast_name") or f"{self._player_id}-Output"
                log.info(f"[NDI SEND] Starting broadcast: {name}")
                self._ndi_sender.start(name)

        elif cmd == "stop_ndi_broadcast":
            if self._ndi_sender:
                log.info("[NDI SEND] Stopping broadcast")
                self._ndi_sender.stop()

        else:
            log.debug(f"Unknown command: {cmd}")

    # ------------------------------------------------------------------
    # LRU garbage collection
    # ------------------------------------------------------------------

    def _gc_cache(self, playlist_data: dict) -> None:
        """Keep current playlist files + up to 1000 recent others (mirrors JS)."""
        try:
            current_files = set()
            for item in playlist_data.get("items", []):
                content_id = item.get("content_id")
                if not content_id:
                    continue
                fn = None
                if item.get("content") and item["content"].get("filename"):
                    fn = item["content"]["filename"]
                if not fn:
                    fn = f"{content_id}.mp4"
                current_files.add(fn)

            file_stats = []
            for f in CACHE_DIR.iterdir():
                if f.is_file() and re.search(
                    r"\.(mp4|webm|mkv|avi|mov|jpg|jpeg|png|gif|bmp|webp|svg)$",
                    f.name, re.IGNORECASE
                ):
                    try:
                        file_stats.append((f.stat().st_mtime, f))
                    except OSError:
                        pass

            # Newest first
            file_stats.sort(reverse=True)

            MAX_UNASSIGNED = 1000
            unassigned = 0
            for _mtime, fp in file_stats:
                if fp.name not in current_files:
                    unassigned += 1
                    if unassigned > MAX_UNASSIGNED:
                        log.info(f"[GC] Deleting old file: {fp.name}")
                        try:
                            fp.unlink()
                        except OSError as e:
                            log.error(f"[GC] Delete error: {e}")
        except Exception as e:
            log.error(f"[GC] Error: {e}")

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    def _suppress_desktop_popups(self) -> None:
        """
        Kill desktop notification daemons and update notifiers so they cannot
        display popups over the fullscreen signage content.
        Called once at startup — these services are not needed on a signage player.
        """
        # Notification daemons (any one of these may be running depending on DE)
        daemons = [
            "dunst",            # lightweight notification daemon
            "notify-osd",       # Ubuntu legacy
            "xfce4-notifyd",    # XFCE
            "mako",             # Wayland
            "swaync",           # Sway notification center
            "xfce4-notifyd",
            "mate-notification-daemon",
            "lxqt-notificationd",
            "cinnamon-notificationd",
        ]
        # Update / package manager popups
        daemons += [
            "update-notifier",
            "update-manager",
            "gnome-software",
            "packageupdater",
            "mintupdate",
        ]
        for name in daemons:
            try:
                subprocess.run(
                    ["pkill", "-x", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

        # Inhibit screensaver / power management popups via xdg-screensaver
        try:
            subprocess.Popen(
                ["xdg-screensaver", "reset"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

        log.info("Desktop popups suppressed")

    async def run(self) -> None:
        """Start the player.  Blocks until shutdown."""
        self._loop = asyncio.get_running_loop()

        # Ensure directories
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # Ensure DISPLAY is set for X11 (required for mpv window on Linux)
        if not os.environ.get("DISPLAY"):
            os.environ["DISPLAY"] = self.display
            log.info(f"Set DISPLAY={self.display}")

        # Kill notification daemons and update popups before taking the screen
        self._suppress_desktop_popups()

        # Start local API server (for CMS to proxy NDI sources, etc.)
        self._local_api.set_player(self)
        self._local_api.start()

        # Initialise mpv
        self.mpv.initialise(self._loop)

        # Clear any stale OSD from a previous run
        self.mpv.clear_osd()

        # Wire up end-of-file → advance
        self.mpv.set_eof_callback(self._on_eof)

        # Validate cms_url
        if not self.cms_url:
            log.error(
                "cms_url is not set in config.yaml.  "
                "Please set cms_url and restart."
            )
            # Show message on screen and wait
            self.mpv.show_osd(
                "ERROR: cms_url not configured.\n"
                "Please set cms_url in config.yaml and restart.",
                duration_ms=3_600_000,
            )
            while self._running:
                await asyncio.sleep(5)
            return

        # Enroll or start player routines
        if self._player_id:
            log.info(f"Resuming with player_id={self._player_id}")
            await self._start_player_routines()
        else:
            log.info("No player_id found — starting enrollment.")
            await self._request_enrollment()

        # Keep running
        while self._running:
            await asyncio.sleep(1)

    async def _on_eof(self) -> None:
        """Called by mpv when a file reaches end-of-file (for videos with duration==0)."""
        if self._playlist_items:
            item = self._playlist_items[self._current_index] \
                if 0 <= self._current_index < len(self._playlist_items) else None
            if item:
                filename = item.get("filename", "")
                duration = item.get("duration", 0) or 0
                # Only auto-advance if not an image and duration==0
                # (images and timed items are handled by the duration timer)
                if not _is_image(filename) and duration == 0:
                    await self._advance()

    def shutdown(self) -> None:
        """Signal the run loop to stop."""
        log.info("Shutting down...")
        self._running = False
        self._cancel_duration_timer()
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        # Stop NDI
        if self._ndi_receiver:
            self._ndi_receiver.stop()
        if self._ndi_sender:
            self._ndi_sender.stop()
        ndi_cleanup()
        self._local_api.stop()
        self.mpv.terminate()
        if self._http and not self._http.is_closed:
            asyncio.create_task(self._http.aclose())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    cfg_raw = load_config()
    player = Player(cfg_raw)

    loop = asyncio.get_running_loop()

    def _sig_handler(sig, _frame):
        log.info(f"Received signal {sig} — shutting down.")
        player.shutdown()
        loop.stop()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    try:
        await player.run()
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())

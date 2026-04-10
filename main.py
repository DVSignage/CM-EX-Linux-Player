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

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"}
DEFAULT_IMAGE_DURATION = 10  # seconds when duration == 0 for an image
LOCAL_API_PORT = 8081  # Local HTTP API (same as Windows player)

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
            keep_open="yes",        # keep window open after file ends; "always" caused a
                                    # spurious second end-file event when transitioning
                                    # between videos, breaking multi-video playlists
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

    def play_file(self, path: str, loop: bool = False) -> None:
        """Load and play a local file path (or URL).

        Sets _deliberate_load=True before calling play() so the end-file
        callback for any currently-playing file is ignored (it was stopped
        intentionally, not via natural EOF).
        """
        if self._mpv:
            self._deliberate_load = True
            try:
                self._mpv.loop_file = "inf" if loop else "no"
            except Exception:
                pass
            self._mpv.play(path)
            self._mpv.pause = False

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

        # NDI state
        self._ndi_receiver: Optional[object] = None
        self._ndi_sender: Optional[object] = None
        self._ndi_active_key: Optional[str] = None  # dedup key for wall commands

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

        log.info(
            f"Playing [{index+1}/{len(self._playlist_items)}] "
            f"{filename}  (duration={duration}, loop={use_loop})"
        )
        self.mpv.play_file(str(path), loop=use_loop)

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
            }
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
                asyncio.create_task(self._fetch_playlist())

        elif cmd == "load_content":
            content_id = data.get("content_id")
            if not content_id:
                return
            if content_id == self.last_content_id:
                log.info("Same content already loaded — skipping.")
            else:
                self.last_content_id = content_id
                self.last_playlist_hash = None
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

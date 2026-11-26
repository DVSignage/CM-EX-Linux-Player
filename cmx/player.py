"""
The player orchestrator: setup, enrollment, heartbeat, SSE push, command
handling, playlist download/apply, templates, interactive walls, live
capture, telemetry, refresh and re-enrollment.

Behaviour mirrors DVSI Plus `Cmx/CmxPlayerService.cs` and the parts of
`CmxPlayerWindow` that are decisions rather than drawing — see README.md
"How it works" and the module docstrings for the detailed rules.
"""

import asyncio
import io
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import urlsplit

import httpx

from . import VERSION
from . import cdp, display
from . import faults as F
from . import inventory as inv
from .browser import Browser, find_chromium
from .cache import CacheStore, PendingDownloads, repair_name
from .cms_client import CmsClient, CmsError, ContentGone, ContentNotReady, RateLimited
from .commands import (ENVELOPE_COMMANDS, RESEND, RUNNING, STATE_COMMANDS, CommandGate, Coalescer,
                       not_supported, parse_seek, parse_volume, try_read_bool, try_read_int)
from .config import Paths, Settings, load_state, save_state, write_json_atomic
from .edge import CONTROL, EdgeRouter
from .enrollment import CODE_POLL_CAP, NEW_CODE_RETRY, POLL_INTERVAL, enroll_link, poll_wait
from .health import address_signature, lan_address_for, metrics, port_holder, port_conflict_message
from .interaction import InteractionEngine, resolve_zone, wall_command_refusal
from .items import TEMPLATE, WEB, Item, classify
from .kiosk import Kiosk
from .lan_api import LanApiServer
from . import ndi_out
from .local_api import PORT as LOCAL_API_PORT, LocalApiServer, load_or_create_key
from .playback import PAUSED, PLAYING, STOPPED, PlaylistEngine
from .preview import PreviewPusher, encode_jpeg
from .surface import MpvSurface, PageSurface
from .telemetry import (CLOUD_TYPES, TelemetryQueue, boot_event, crash_event, edge_event,
                        pending_crash_markers, ts)
from .templates import TemplateBuilder, av_offset, patch_definition
from .wall import STREAM_TUNING, crop_filter_normalised, crop_filter_pixel, normalise_crop, stream_url_with_buffers
from .wallhost import WallHost

try:
    from ndi import engine as ndi_engine
except Exception:   # optional
    ndi_engine = None

log = logging.getLogger("player")

HEARTBEAT_INTERVAL = 0.5
RECONNECT_INTERVAL = 15.0
BEAT_DEADLINE = 40.0
PROBE_TIMEOUT = 5.0
SSE_RECONNECT = 3.0
PENDING_INTERVAL = 15.0
TELEMETRY_INTERVAL = 60.0
HEALTH_INTERVAL = 60.0
INFO_INTERVAL = 30.0
NET_WATCH_INTERVAL = 2.0
RSS_ONLINE, RSS_OFFLINE = 60.0, 900.0
EDGE_PROBE_INTERVAL = 30.0
STALE_404_HOLD = 120.0
EMPTY_REFETCH_THROTTLE = 3.0
OFFLINE_NOTICE_SECONDS = 6.0
COMMAND_LOG_SUMMARY = 30.0
PLAYLIST_FETCH_FAULT_AFTER = 3
PROOF_MIN_SECONDS = 0.25
WEB_PROBE_TTL = 600.0
PORT_RETRY_INTERVAL = 30.0


def _ndi_available() -> bool:
    try:
        return bool(ndi_engine and ndi_engine.is_available())
    except Exception:
        return False


def _host_port(url: Optional[str]) -> Optional[str]:
    try:
        u = urlsplit(url or "")
        return f"{(u.hostname or '').lower()}:{u.port or ''}" if u.hostname else None
    except ValueError:
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Player:
    def __init__(self, settings: Settings, paths: Paths, *,
                 transport: Optional[httpx.AsyncBaseTransport] = None,
                 mpv=None, browser=None, surface=None,
                 enable_local_api: bool = True, enable_kiosk: bool = True):
        self.settings = settings
        self.paths = paths
        paths.ensure()
        self.state: dict = load_state(paths)
        self._adopt_config_url()
        self._transport = transport

        self.cache = CacheStore(paths.cache_dir, paths.cache_history, settings.keep_playlists)
        self.pending = PendingDownloads()
        self.faults = F.PlayerFaults()
        self.gate = CommandGate()
        self.coalescer = Coalescer()
        self.telemetry = TelemetryQueue(paths.telemetry_queue, on_drop=self._telemetry_dropped)
        self._build_cms()

        # Display: the renderer page in a Chromium kiosk, or mpv alone.
        self.renderer = None
        self.kiosk: Optional[Kiosk] = None
        self.mpv = mpv
        self._wall_mpv = None
        if surface is not None:
            self.surface = surface
        elif mpv is None and browser is None and settings.renderer != "mpv" and find_chromium():
            from .renderer.server import RendererServer
            self.renderer = RendererServer(paths.cache_dir, paths.screens_dir, paths.rss_dir, self._page_event_threadsafe)
            self.surface = PageSurface(self.renderer, web_probe=self._web_probe)
            self.surface.window = Browser(settings.display, paths.browser_profile)
            if enable_kiosk:
                self.kiosk = Kiosk(settings.display, paths.kiosk_profile, self.renderer.url, self._renderer_recovered)
        else:
            if self.mpv is None:
                from .playback import MpvPlayer
                self.mpv = MpvPlayer(settings.display, settings.audio_output)
            self.surface = MpvSurface(self.mpv, browser or Browser(settings.display, paths.browser_profile),
                                      screens_dir=paths.screens_dir, crop_filter=crop_filter_normalised)
        self.page_mode = isinstance(self.surface, PageSurface)

        self.engine = PlaylistEngine(
            self.surface,
            default_image=settings.default_image_duration,
            resolve=self._resolve,
            on_waiting=self._show_waiting,
            on_item=self._on_item,
            screen_size=self._screen_size,
        )
        self.preview = PreviewPusher(self._capture_frame, self._post_frame)
        self._enable_local_api = enable_local_api
        self.local_api: Optional[LocalApiServer] = None
        self.lan_api: Optional[LanApiServer] = None

        self.volume = int(self.state.get("volume", 100))
        self.muted = bool(self.state.get("muted", False))

        # Dedup / command state
        self.last_playlist_hash: Optional[str] = None
        self.last_content_id: Optional[str] = None
        self._content_hash: Optional[str] = None
        self._playlist_uuid: Optional[str] = None
        self._last_sent_key: Optional[str] = None
        self._last_empty_fetch = 0.0
        self._wall_key: Optional[str] = None
        self._ndi_active_key: Optional[str] = None
        self._live_key: Optional[str] = None
        self._last_blank: Optional[bool] = None
        self._brightness_logged = False
        self._refreshing = False
        self._fetch_failures = 0
        self._gone: set = set()
        self._dl_locks: Dict[str, asyncio.Lock] = {}
        self._fetch_lock = asyncio.Lock()
        self._fetch_task: Optional[asyncio.Task] = None
        self._cmd_log_key: Optional[str] = None
        self._cmd_log_repeats = 0
        self._cmd_log_since = 0.0
        self._rss_feeds: Set[str] = set()
        self._web_probe_cache: Dict[str, tuple] = {}
        self.capture_devices: List[dict] = []
        self._live_trim = 0
        self.av_offset_ms: Optional[int] = None
        self.av_hold_ms: Optional[int] = None

        # Walls and proof of play
        self.wall: Optional[InteractionEngine] = None
        self.wall_host: Optional[WallHost] = None
        self._wall_item: Optional[Item] = None
        self._play_started: Optional[tuple] = None   # (utc datetime, monotonic)

        # Lifecycle
        self.online: Optional[bool] = None
        self.exit_code = 0
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._rss_wake = asyncio.Event()
        self._telemetry_kick = asyncio.Event()
        self._routines: Dict[str, asyncio.Task] = {}
        self._enroll_task: Optional[asyncio.Task] = None
        self._wall_task: Optional[asyncio.Task] = None
        self._overlay_timer: Optional[asyncio.TimerHandle] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._boot_reported = False
        self.port_conflicts: Dict[str, dict] = {}     # service -> {port, holder, reported}
        self._port_task: Optional[asyncio.Task] = None
        self.started_at = _utc_now()
        self._started_wall = time.time()
        self.local_ip: Optional[str] = None
        self.ndi_sources: List[dict] = []
        self._ndi_receiver = ndi_engine.NDIReceiver() if ndi_engine else None
        self._ndi_sender = ndi_engine.NDISender() if ndi_engine else None
        self.ndi_output = ndi_out.NdiOutput(self._ndi_sender, self._make_ndi_source, settings.ndi_fps)
        self._cms_ndi: Optional[bool] = None
        self._cms_ndi_name: Optional[str] = None
        self._ndi_refused: Optional[tuple] = None
        self.inventory = inv.InventoryReporter(paths.inventory_state)

    # ------------------------------------------------------------------
    # Identity, connection
    # ------------------------------------------------------------------

    @property
    def player_id(self) -> Optional[str]:
        return self.state.get("player_id")

    @property
    def cms_url(self) -> str:
        return self.settings.cms_url or str(self.state.get("cms_url") or "")

    @property
    def connection_mode(self) -> str:
        m = self.settings.connection_mode or str(self.state.get("connection_mode") or "cloud")
        return m if m in ("cloud", "edge") else "cloud"

    @property
    def edge_url(self) -> str:
        return self.settings.edge_url or str(self.state.get("edge_url") or "")

    @property
    def edge_mode(self) -> bool:
        return self.cms.router.enabled

    def _adopt_config_url(self) -> None:
        """A config.yaml cms_url on a different host means a new registration."""
        url = self.settings.cms_url
        if not url:
            return
        saved = self.state.get("cms_url")
        if saved and _host_port(saved) != _host_port(url) and self.state.get("player_id"):
            log.warning(f"CMS changed ({saved} → {url}) — this screen will register again")
            for k in ("player_id", "player_secret", "site_id", "site_name"):
                self.state.pop(k, None)
        self.state["cms_url"] = url
        save_state(self.paths, self.state)

    def _build_cms(self) -> None:
        router = EdgeRouter(self.connection_mode, self.edge_url, self.cms_url, self._edge_transition)
        self.cms = CmsClient(self.cms_url, lambda: self.state.get("player_secret"), self._transport, router)
        # Cloud mode sends only edge_event and inventory; proof-of-play and
        # player_health are the edge's (held, never dropped, otherwise).
        self.telemetry.routable = ({"edge_event", "inventory", "proof_of_play", "player_health"}
                                   if router.enabled else set(CLOUD_TYPES))

    def _save(self) -> None:
        save_state(self.paths, self.state)

    def _screen_size(self) -> tuple:
        if self.mpv is not None and hasattr(self.mpv, "display_size"):
            return self.mpv.display_size()
        return (1920, 1080)

    # ------------------------------------------------------------------
    # Screens and notices
    # ------------------------------------------------------------------

    def _show_message(self, name: str, title: str, body: str = "") -> None:
        self.surface.show_screen("message", title=title, body=body)

    def _show_waiting(self) -> None:
        self._show_message("waiting", "CMX Player — waiting for content…")

    def _show_enrollment(self, code: str) -> None:
        server = self.cms_url if not self.edge_mode else f"{self.edge_url} (cloud: {self.cms_url} failover)"
        self.surface.show_screen("enrollment", code=code, server=server, site=self.state.get("site_name"),
                                 link=enroll_link(self.cms_url, code, self.state.get("site_id")))

    def _overlay(self, kind: Optional[str], title: str = "", body: str = "", opaque: bool = True,
                 hide_after: Optional[float] = None) -> None:
        if self._overlay_timer:
            self._overlay_timer.cancel()
            self._overlay_timer = None
        self.surface.overlay(kind, title, body, opaque)
        if kind and hide_after and self._loop:
            self._overlay_timer = self._loop.call_later(hide_after, lambda: self.surface.overlay(None))

    # ------------------------------------------------------------------
    # Run / shutdown
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> int:
        self._loop = loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError, ValueError):
                pass

        if sys.platform.startswith("linux"):
            # The service can start before the desktop session; on GNOME/Wayland the
            # XWayland auth file has a new name at every login (cmx/display.py).
            info = await display.wait_for_display(self.settings.display)
            log.info(f"[DISPLAY] {info['session']} session, DISPLAY={info['display']}, "
                     f"XAUTHORITY={info['xauthority'] or '(none)'}")
            await asyncio.to_thread(display.keep_desktop_awake)
        elif not os.environ.get("DISPLAY"):
            os.environ["DISPLAY"] = self.settings.display
        _suppress_desktop_popups()

        if self._enable_local_api:
            self.local_api = LocalApiServer(self.paths.local_api_key, self._device_info,
                                            self._ndi_source_list, self._snapshot_jpeg)
            self._bound("local API", self.local_api.port, self.local_api.start())
            self.lan_api = LanApiServer(
                load_or_create_key(self.paths.local_api_key),
                lan_enabled=self.settings.lan_triggers, trigger_key=self.settings.trigger_key,
                handle_wall=self._lan_wall_threadsafe, snapshot=self._lan_snapshot_threadsafe,
                control=self._lan_control_threadsafe, device_info=self._device_info,
                capture_devices=lambda: list(self.capture_devices),
                ndi_sources=lambda: {"available": _ndi_available(),
                                     "reason": None if _ndi_available() else "NDI runtime (ndi-python) not installed",
                                     "sources": [{"name": s.get("name")} for s in self._ndi_source_list()]},
                preview_jpeg=self._snapshot_jpeg,
                reset_registration=lambda: self._on_loop(self.reset_registration("local API"), 60))
            self._bound("control API", self.lan_api.port, self.lan_api.start())

        if self.renderer is not None:
            self._bound("renderer page", self.renderer.port, self.renderer.start())
            if self.kiosk is not None:
                self.kiosk.url = self.renderer.url   # the port may have moved off a taken 8090
            self.surface.send({"type": "config", "loading_indicator": self.settings.loading_indicator,
                               "music_wave": self.settings.music_wave,
                               "capture_max_height": self.settings.capture_max_height})
            if self.kiosk is not None:
                self.kiosk.start()
        elif self.mpv is not None and hasattr(self.mpv, "initialise"):
            self.mpv.initialise(loop)
        self.surface.set_volume(self.volume, self.muted)
        self.surface.clear_osd()
        if self.settings.ndi_output:
            self._apply_ndi_output()

        self._port_task = asyncio.ensure_future(self._port_retry_loop())

        if not self.cms_url:
            log.warning("No CMS address yet — showing the setup screen.")
            self.surface.show_screen("setup", mode=self.connection_mode, cloud="", edge=self.edge_url)
        elif self.player_id:
            log.info(f"Resuming with player_id={self.player_id}")
            await self.startup()
        else:
            log.info("No player_id found — starting enrollment.")
            self._enroll_task = asyncio.ensure_future(self.enroll())

        await self._stop.wait()
        await self.shutdown()
        return self.exit_code

    async def shutdown(self) -> None:
        log.info("Shutting down...")
        for t in list(self._routines.values()) + [self._enroll_task, self._wall_task, self._fetch_task, self._port_task]:
            if t and not t.done():
                t.cancel()
        self.preview.stop()
        try:
            await self.engine.release()
        except Exception:
            pass
        if self.kiosk is not None:
            await self.kiosk.stop()
        if self.renderer is not None:
            self.renderer.stop()
        self.ndi_output.stop()
        if self._ndi_receiver:
            self._ndi_receiver.stop()
        if self._ndi_sender:
            self._ndi_sender.stop()
        if ndi_engine:
            ndi_engine.cleanup()
        if self.local_api:
            self.local_api.stop()
        if self.lan_api:
            self.lan_api.stop()
        self.telemetry.save()
        for m in (self.mpv, self._wall_mpv):
            if m is not None and hasattr(m, "terminate"):
                m.terminate()
        await self.cms.aclose()

    # ------------------------------------------------------------------
    # Setup screen
    # ------------------------------------------------------------------

    async def _on_setup(self, msg: dict) -> None:
        mode = msg.get("mode") if msg.get("mode") in ("cloud", "edge") else "cloud"
        cloud = str(msg.get("cms_url") or "").strip().rstrip("/")
        edge = str(msg.get("edge_url") or "").strip().rstrip("/")
        if not cloud.startswith(("http://", "https://")) or (mode == "edge" and not edge.startswith(("http://", "https://"))):
            self.surface.show_screen("setup", mode=mode, cloud=cloud, edge=edge, error="Enter a valid server address.")
            return
        if self.player_id and _host_port(self.state.get("cms_url")) != _host_port(cloud):
            for k in ("player_id", "player_secret", "site_id", "site_name"):
                self.state.pop(k, None)
        self.state.update(cms_url=cloud, connection_mode=mode, edge_url=edge if mode == "edge" else "")
        self._save()
        await self.cms.aclose()
        self._build_cms()
        log.info(f"[SETUP] {mode} connection: cloud={cloud}{' edge=' + edge if mode == 'edge' else ''}")
        if self.player_id:
            await self.startup()
        elif self._enroll_task is None or self._enroll_task.done():
            self._enroll_task = asyncio.ensure_future(self.enroll())

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Play the offline copy first, then ask the CMS whether this screen
        is still registered (bounded to 5 s so an unreachable host cannot
        stall startup)."""
        played = await self._play_offline()
        if not played:
            self._show_message("connecting", "Connecting to the server…")
        status = await self._probe()
        if status == "unregistered":
            await self.re_enroll()
            return
        self.start_routines()
        if self.edge_mode:
            asyncio.ensure_future(self._check_site())
        if status == "registered":
            self._set_online(True, fetch=False)
            self.spawn_fetch()
        else:
            self._set_online(False)

    def _probe_body(self) -> dict:
        return {"status": "online", "timestamp": _utc_now().isoformat(),
                "capture_devices": [], "decklink_devices": [], "decklink_supported": False,
                "ndi_sources": [], "ndi_supported": False}

    async def _probe(self) -> str:
        try:
            r = await asyncio.wait_for(self.cms.heartbeat(self.player_id, self._probe_body(), timeout=PROBE_TIMEOUT),
                                       PROBE_TIMEOUT + 1)
        except Exception as e:
            log.info(f"[STARTUP] CMS unreachable ({type(e).__name__}) — playing offline")
            return "offline"
        if r.status_code == 404:
            if self.cms.last_from_edge:
                verdict = await self._confirm_deleted()
                return {"deleted": "unregistered", "alive": "registered"}.get(verdict, "offline")
            return "unregistered"
        return "registered" if 200 <= r.status_code < 300 else "offline"

    async def _confirm_deleted(self) -> str:
        """An edge 404 may be a fresh replica that has not synced this player
        yet: ask the cloud directly before wiping anything."""
        try:
            r = await self.cms.heartbeat_cloud(self.player_id, self._probe_body())
        except Exception:
            return "inconclusive"
        if r.status_code == 404:
            return "deleted"
        if 200 <= r.status_code < 300:
            self.cms.router.force_failover(CONTROL, STALE_404_HOLD, "the edge does not know this player yet")
            self._emit_edge_event("warn", "edge_stale_404", "edge answered 404 for a player the cloud knows",
                                  {"hold_s": STALE_404_HOLD})
            return "alive"
        return "inconclusive"

    async def _check_site(self) -> None:
        info = await self.cms.site_info()
        if info is None:
            log.warning("[EDGE] the edge server did not describe its site (GET /api/v1/site)")
            return
        mine = self.state.get("site_id")
        log.info(f"[EDGE] edge serves site {info.get('site_id')} (enrolled={info.get('enrolled')})")
        if mine and info.get("site_id") and info.get("site_id") != mine:
            log.warning(f"[EDGE] this player belongs to site {mine} but the edge serves {info.get('site_id')}")
        if info.get("enrolled") is False:
            log.warning("[EDGE] the edge server is not enrolled with the cloud")

    def start_routines(self) -> None:
        for t in self._routines.values():
            if t and not t.done() and t is not asyncio.current_task():
                t.cancel()
        self._routines = {
            "heartbeat": asyncio.ensure_future(self._heartbeat_loop()),
            "sse": asyncio.ensure_future(self._sse_loop()),
            "pending": asyncio.ensure_future(self._pending_loop()),
            "telemetry": asyncio.ensure_future(self._telemetry_loop()),
            "info": asyncio.ensure_future(self._info_loop()),
            "net": asyncio.ensure_future(self._net_watch_loop()),
            "rss": asyncio.ensure_future(self._rss_loop()),
            "wall": asyncio.ensure_future(self._wall_tick_loop()),
            "inventory": asyncio.ensure_future(self._inventory_loop()),
        }
        if self.edge_mode:
            self._routines["health"] = asyncio.ensure_future(self._health_loop())
            self._routines["edge_probe"] = asyncio.ensure_future(self._edge_probe_loop())
        self._queue_stability_events()

    # ------------------------------------------------------------------
    # Enrollment
    # ------------------------------------------------------------------

    async def enroll(self) -> None:
        """Request a code, show it, poll with the same code until approved,
        expired/rejected or 10 minutes pass."""
        self._show_message("connecting", "Connecting to the server…")
        while not self._stop.is_set():
            try:
                data = await self.cms.enroll_request()
                code = str(data["code"])
            except Exception as e:
                log.error(f"Enrollment request failed: {e} — retrying in {NEW_CODE_RETRY:.0f}s")
                await asyncio.sleep(NEW_CODE_RETRY)
                continue
            self._capture_site(data)
            if self.edge_mode and not data.get("site_id"):
                log.warning("[EDGE] the code came back without a site: this player will replicate to no edge")
            log.info(f"Enrollment code: {code}")
            self._show_enrollment(code)
            if await self._poll_code(code):
                return

    async def _poll_code(self, code: str) -> bool:
        started = time.monotonic()
        wait = POLL_INTERVAL
        transport_logged = False
        while not self._stop.is_set():
            await asyncio.sleep(wait)
            wait = POLL_INTERVAL
            if time.monotonic() - started > CODE_POLL_CAP:
                log.info("Enrollment code unanswered for 10 minutes — requesting a new one")
                return False
            try:
                status, body, retry_after = await self.cms.enroll_status(code)
            except Exception as e:
                if not transport_logged:
                    log.warning(f"Enrollment poll failed ({e}) — keeping code {code}")
                    transport_logged = True
                continue
            if status == 200:
                s = str(body.get("status") or "")
                if s == "approved" and body.get("player_id"):
                    await self._approved(body)
                    return True
                if s in ("expired", "rejected"):
                    log.warning(f"Enrollment {s} — requesting a new code")
                    return False
                continue
            pending = poll_wait(status, retry_after)
            if pending is not None:
                wait = pending
            else:
                log.warning(f"Enrollment poll answered HTTP {status} — keeping code {code}")
        return False

    def _capture_site(self, data: dict) -> None:
        for k in ("site_id", "site_name"):
            if data.get(k):
                self.state[k] = data[k]

    async def _approved(self, body: dict) -> None:
        log.info(f"Approved!  player_id={body['player_id']}")
        self.state["player_id"] = str(body["player_id"])
        if body.get("player_secret"):
            self.state["player_secret"] = body["player_secret"]
        self._capture_site(body)
        self._save()
        self.surface.clear_osd()
        self._show_waiting()
        self.start_routines()
        self._set_online(True, fetch=False)
        self.spawn_fetch()

    async def reset_registration(self, source: str = "screen") -> str:
        """Forget this screen's registration (DVSI Plus ResetRegistration): identity,
        secret, site, cache and the saved server addresses — the connection mode
        is kept so the setup screen reopens on it. With cms_url in config.yaml
        the address cannot be forgotten, so a fresh code is shown instead."""
        log.warning(f"[RESET] registration reset from the {source}")
        current = asyncio.current_task()
        for t in list(self._routines.values()) + [self._enroll_task]:
            if t and not t.done() and t is not current:
                t.cancel()
        self._routines = {}
        self._enroll_task = None
        self._leave_walls()
        self.preview.stop()
        await self.engine.release()
        self.cache.clear()
        try:
            self.paths.offline_playlist.unlink()
        except OSError:
            pass
        for k in ("player_id", "player_secret", "site_id", "site_name"):
            self.state.pop(k, None)
        if not self.settings.cms_url:
            self.state.pop("cms_url", None)
            self.state.pop("edge_url", None)
        self._save()
        self.pending = PendingDownloads()
        self._gone.clear()
        self.last_playlist_hash = self.last_content_id = self._last_sent_key = None
        self.online = None
        await self.cms.aclose()
        self._build_cms()
        if self.cms_url:
            self._enroll_task = asyncio.ensure_future(self.enroll())
            return "registration reset — showing a new code"
        self.surface.show_screen("setup", mode=self.connection_mode, cloud="", edge="")
        return "registration reset — showing the setup screen"

    async def re_enroll(self) -> None:
        """Unregistered (heartbeat 404 or delete_player): drop the identity,
        the cache and the offline copy, and show a fresh code."""
        log.warning("[RE-ENROLL] This player is not registered — clearing state and re-enrolling.")
        current = asyncio.current_task()
        for t in self._routines.values():
            if t and not t.done() and t is not current:
                t.cancel()
        self._routines = {k: v for k, v in self._routines.items() if v is current}
        self._cancel_wall()
        self.preview.stop()
        self.state.pop("player_id", None)
        self._save()
        await self.engine.release()
        self.cache.clear()
        try:
            self.paths.offline_playlist.unlink()
        except OSError:
            pass
        self.pending = PendingDownloads()
        self._gone.clear()
        self.last_playlist_hash = self.last_content_id = self._last_sent_key = None
        self._wall_key = self._ndi_active_key = self._live_key = None
        self.online = None
        if self._enroll_task is None or self._enroll_task.done():
            self._enroll_task = asyncio.ensure_future(self.enroll())

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            delay = HEARTBEAT_INTERVAL if self.online else RECONNECT_INTERVAL
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if not self.player_id:
                continue
            try:
                await asyncio.wait_for(self.beat(), BEAT_DEADLINE)
            except asyncio.TimeoutError:
                log.warning("Heartbeat took too long — treating as offline")
                self._set_online(False)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"Heartbeat error: {e}", exc_info=True)

    async def beat(self) -> None:
        pid = self.player_id
        try:
            r = await self.cms.heartbeat(pid, self.heartbeat_body())
        except (httpx.TransportError, httpx.TimeoutException) as e:
            if self.online is not False:
                log.warning(f"Heartbeat failed: {e}")
            self._set_online(False)
            return
        if r.status_code == 404:
            if self.cms.last_from_edge:
                verdict = await self._confirm_deleted()
                if verdict == "deleted":
                    await self.re_enroll()
                elif verdict == "inconclusive":
                    self._set_online(False)
                return
            await self.re_enroll()
            return
        if r.status_code == 409:
            log.warning(f"Heartbeat refused (409): {r.text[:300]} Retry-After={r.headers.get('Retry-After')}")
            self._set_online(False)
            return
        if not 200 <= r.status_code < 300:
            log.warning(f"Heartbeat answered HTTP {r.status_code}")
            self._set_online(False)
            return
        self._set_online(True)
        try:
            data = r.json() if r.content and r.content.strip() else {}
        except ValueError:
            data = {}
        if isinstance(data, dict):
            await self.handle_response(data)

    def heartbeat_body(self) -> dict:
        st = self.engine.status()
        live = (self._wall_task and not self._wall_task.done()) or self._live_key or self._ndi_active_key
        if live:
            st.update(status="playing", playback_status="playing", position=None, duration=None)
        body = {
            "status": st["status"],
            "playback_status": st["playback_status"],
            "position": st["position"],
            "duration": st["duration"],
            "timestamp": _utc_now().isoformat(),
            "current_content_id": st["current_content_id"],
            "current_content_name": st["current_content_name"],
            "playlist_index": st["playlist_index"],
            "playlist_total": st["playlist_total"],
            "scene_id": self.wall.active_scene_id if self.wall else None,
            "home_scene_id": self.wall.current_home if self.wall else None,
            "local_ip": self.local_ip,
            "api_port": LOCAL_API_PORT,
            "player_version": VERSION,
            "uptime_seconds": int(time.time() - self._started_wall),
            "error": self.compute_error(),
            "capture_devices": list(self.capture_devices),
            "decklink_devices": [],
            "decklink_supported": False,
            "decklink_reason": "DeckLink capture is not supported by the Linux player",
            "ndi_available": _ndi_available(),
            "ndi_sources": [{"name": s.get("name")} for s in self.ndi_sources if s.get("name")],
            "ndi_supported": _ndi_available(),
            "ndi_reason": None if _ndi_available() else "NDI runtime (ndi-python) not installed",
            "av_hold_ms": self.av_hold_ms if self._live_key else None,
            "av_offset_ms": self.av_offset_ms if self._live_key else None,
        }
        body.update(metrics())
        if self.ndi_output.active:
            body["ndi_broadcast_active"] = True
            body["ndi_broadcast_name"] = self.ndi_output.name
        if self._ndi_receiver and self._ndi_receiver.active:
            body["ndi_receive_active"] = True
            body["ndi_receive_source"] = self._ndi_receiver.source_name
        return body

    def compute_error(self) -> Optional[str]:
        self.faults.put(F.DOWNLOADS, self.pending.fault())
        items = self.engine.items
        if items:
            failed = [it for it in items if it.is_media and (it.content_id in self._gone
                      or it.content_id in self.pending.entries) and not self.engine.ready(it)]
            if failed and len(failed) == len(items):
                self.faults.set(F.NOTHING_PLAYS, F.nothing_plays_message(len(items), "their files could not be downloaded"))
                self.faults.clear(F.ITEMS_FAIL)
            elif failed:
                self.faults.clear(F.NOTHING_PLAYS)
                self.faults.set(F.ITEMS_FAIL, F.items_fail_message(len(failed), len(items), failed[0].name))
            else:
                self.faults.clear(F.NOTHING_PLAYS)
                self.faults.clear(F.ITEMS_FAIL)
        else:
            self.faults.clear(F.NOTHING_PLAYS)
            self.faults.clear(F.ITEMS_FAIL)
        if self.kiosk is not None and self.kiosk.failing:
            self.faults.set(F.RENDERER, F.renderer_message(
                f"{self.kiosk.recoveries_in_window} recoveries in 5 min"))
        elif self.kiosk is not None and self.kiosk.last_error:
            self.faults.set(F.RENDERER, F.renderer_message(self.kiosk.last_error))
        else:
            self.faults.clear(F.RENDERER)
        return self.faults.top()

    def _set_online(self, online: bool, fetch: bool = True) -> None:
        prev, self.online = self.online, online
        if online and prev is not True:
            if prev is False:
                log.info("CMS reachable again")
                if fetch:
                    self.spawn_fetch()
                self._rss_wake.set()
            self._overlay(None)
            if not self.engine.has_content and not (self._wall_task and not self._wall_task.done()) \
                    and not self._live_key:
                self._show_waiting()
        elif not online and prev is not False:
            log.warning("CMS unreachable — offline")
            if self.engine.has_content:
                self._overlay("offline", "Server offline",
                              "No problem — playing your saved content. It'll update automatically when the server is back online.",
                              opaque=False, hide_after=OFFLINE_NOTICE_SECONDS)
            else:
                self._show_message("offline", "Server offline",
                                   "Trying to reconnect… content will start once the server is back online.")

    # ------------------------------------------------------------------
    # Responses and commands
    # ------------------------------------------------------------------

    async def handle_response(self, data: dict, channel: str = "heartbeat") -> None:
        """Command, then desired state, preview hint and crop — in that order."""
        await self.handle_command(data, channel)
        if channel != "heartbeat":
            return
        await self._apply_desired_state(data)
        self.preview.on_hint(data.get("preview_watch"))
        if "crop" in data:
            self.engine.set_crop(normalise_crop(data.get("crop")))

    def _log_command(self, cmd: str, data: dict, channel: str) -> None:
        shown = {k: v for k, v in data.items() if k != "server_time"}
        key = f"{channel}:{json.dumps(shown, sort_keys=True, default=str)}"
        now = time.monotonic()
        if key == self._cmd_log_key:
            self._cmd_log_repeats += 1
            if now - self._cmd_log_since >= COMMAND_LOG_SUMMARY:
                log.info(f"[CMD {channel}] '{cmd}' ×{self._cmd_log_repeats} identical repeats")
                self._cmd_log_repeats, self._cmd_log_since = 0, now
            return
        if self._cmd_log_repeats:
            log.info(f"[CMD] previous command repeated ×{self._cmd_log_repeats}")
        self._cmd_log_key, self._cmd_log_repeats, self._cmd_log_since = key, 0, now
        log.info(f"[CMD {channel}] {cmd} {json.dumps(shown, default=str)[:600]}")

    async def handle_command(self, data: dict, channel: str = "heartbeat") -> None:
        cmd = str(data.get("command") or "").strip()
        if not cmd or cmd == "none":
            return
        self._log_command(cmd, data, channel)
        cid = data.get("command_id")
        has_id = cid is not None and cid != ""
        args = dict(data)
        if isinstance(data.get("command_args"), dict):
            args.update(data["command_args"])

        if has_id:
            verdict, recorded = self.gate.claim(cid)
            if verdict == RESEND:
                await self._ack(cid, cmd, *recorded)
                return
            if verdict == RUNNING:
                return

        if cmd in ENVELOPE_COMMANDS:
            if not has_id:
                if self.coalescer.should_run(cmd):
                    await self._execute(cmd, args)
                return
            ok, msg, after = await self._execute(cmd, args)
            self.gate.record(cid, ok, msg)
            await self._ack(cid, cmd, ok, msg)
            if after:
                await after()
            return

        if cmd in STATE_COMMANDS:
            if has_id:
                if cmd == "show_ndi" and not (args.get("source_name") or args.get("ndi_source_name")):
                    self.gate.record(cid, False, "show_ndi needs 'source_name'")
                    await self._ack(cid, cmd, False, "show_ndi needs 'source_name'")
                    return
                self.gate.record(cid, True, f"{cmd} accepted")
                await self._ack(cid, cmd, True, f"{cmd} accepted")
            await self._state_command(cmd, args)
            return

        log.warning(f"UNHANDLED CMS command '{cmd}': {json.dumps(data, default=str)[:600]}")
        if has_id:
            msg = not_supported(cmd)
            self.gate.record(cid, False, msg)
            await self._ack(cid, cmd, False, msg)

    async def _ack(self, command_id, command: str, success: bool, message: str) -> None:
        if not self.player_id:
            return
        if await self.cms.ack(self.player_id, command_id, command, success, message):
            log.info(f"[ACK] {command} ({command_id}): {'ok' if success else 'refused'} — {message}")

    async def _execute(self, cmd: str, args: dict):
        """Run an envelope command → (success, message, after-ack action)."""
        e = self.engine
        if cmd == "play":
            await e.play()
            return True, "playing", None
        if cmd == "pause":
            e.pause()
            return True, "paused", None
        if cmd == "stop":
            await e.stop()
            return True, "stopped", None
        if cmd in ("replay", "restart"):
            if e.mode == STOPPED and e.items:
                await e.play()
                return True, "replaying", None
            ok, msg = await e.replay()
            if ok and self.wall is not None:
                self.wall.reset()
            return ok, msg, None
        if cmd in ("next", "previous"):
            if not e.items:
                return False, "no playlist loaded", None
            await (e.advance() if cmd == "next" else e.previous())
            return True, cmd, None
        if cmd == "seek":
            ok, msg = e.seek(*parse_seek(args))
            return ok, msg, None
        if cmd == "refresh":
            if self._refreshing:
                return False, "refresh already in progress", None
            ok, msg = await self.refresh()
            return ok, msg, None
        if cmd == "restart_service":
            return True, "Restart acknowledged", self._restart_service
        if cmd == "set_volume":
            vol, muted = parse_volume(args, self.volume, self.muted)
            self._apply_volume(vol, muted)
            return True, f"volume {vol}{' (muted)' if muted else ''}", None
        if cmd == "delete_player":
            return True, "re-enrolling", self.re_enroll
        if cmd in ("trigger", "set_scene", "wall_play", "alignment"):
            ok, msg = self._wall_command(cmd, args)
            return ok, msg, None
        if cmd == "template_patch":
            ok, msg = await self._template_patch(args)
            return ok, msg, None
        return False, not_supported(cmd), None

    def _apply_volume(self, volume: int, muted: bool) -> None:
        self.volume, self.muted = volume, muted
        self.surface.set_volume(volume, muted)
        self.state["volume"], self.state["muted"] = volume, muted
        self._save()
        log.info(f"Volume {volume}{' (muted)' if muted else ''}")

    async def _restart_service(self) -> None:
        """Exit after the ack; systemd (Restart=always) starts us again."""
        log.info("[RESTART] restart_service — exiting for systemd to restart the player")
        try:
            await asyncio.wait_for(self._flush_telemetry(), 3)
        except Exception:
            pass
        self.exit_code = 0
        self.request_stop()

    async def _apply_desired_state(self, data: dict) -> None:
        if data.get("audio_volume") is not None or data.get("audio_muted") is not None:
            vol, muted = parse_volume({"volume": data.get("audio_volume"), "muted": data.get("audio_muted")},
                                      self.volume, self.muted)
            if (vol, muted) != (self.volume, self.muted):
                self._apply_volume(vol, muted)
        if data.get("desired_blank") is not None:
            blank = try_read_bool(data.get("desired_blank"))
            if blank is not None and blank != self._last_blank:
                if blank:
                    await self.engine.stop()
                else:
                    await self.engine.play()
                self._last_blank = blank
        if "ndi_broadcast_active" in data:
            want = try_read_bool(data.get("ndi_broadcast_active"))
            name = data.get("ndi_broadcast_name") if isinstance(data.get("ndi_broadcast_name"), str) else None
            # a rename is a change: the name is how other systems find this source
            if want is not None and (want != self._cms_ndi or (want and name != self._cms_ndi_name)):
                await self._set_cms_ndi(want, name)
        if data.get("desired_brightness") is not None and not self._brightness_logged:
            log.info(f"desired_brightness={data['desired_brightness']} (no backlight control on this player)")
            self._brightness_logged = True

    # -- walls --------------------------------------------------------------

    def _template_on_screen(self) -> Optional[Item]:
        cur = self.engine.current
        if cur is not None and cur.kind == TEMPLATE and self.engine.mode in (PLAYING, PAUSED):
            return cur
        return None

    def _wall_command(self, cmd: str, args: dict, source: str = "cms"):
        tpl = self._template_on_screen()
        refusal = wall_command_refusal(args, tpl.template_id if tpl else None)
        if refusal:
            return False, refusal
        if self.wall is None:
            return False, "no interactive template on screen"
        if cmd == "trigger":
            tid = try_read_int(args.get("id") if args.get("id") is not None else args.get("trigger_id"))
            if tid is None:
                return False, "trigger needs a numeric 'id'"
            return self.wall.trigger(tid, source)
        if cmd == "set_scene":
            target = args.get("scene_id") or args.get("scene")
            if not target:
                return False, "set_scene needs 'scene_id'"
            return self.wall.set_scene(str(target), source, bool(try_read_bool(args.get("stop"))))
        if cmd == "wall_play":
            return self.wall.play_content(args.get("content_id") or args.get("contentId"), source)
        return self.wall.set_alignment(bool(try_read_bool(args.get("enabled"))))

    def _attach_wall(self, item: Optional[Item]) -> None:
        if item is self._wall_item:
            return
        self.wall = self.wall_host = None
        self._wall_item = None
        if item is None or item.kind != TEMPLATE or not self.page_mode:
            return
        built = item.extra.get("built")
        if not built or built.get("model") is None:
            return
        host = WallHost(self.surface.send, built["zones"], built["media"],
                        paused=lambda: self.engine.mode == PAUSED)
        engine = InteractionEngine(
            built["model"], built["media"], host, _utc_now,
            zone=resolve_zone(built["payload"].get("timezone"), log.info),
            emit=lambda level, code, message, detail: self._emit_edge_event(level, code, message, detail),
            log=log.info)
        host.engine = engine
        self.wall, self.wall_host, self._wall_item = engine, host, item
        engine.start()
        log.info(f"[WALL] interactive template {item.template_id}: scene '{engine.active_scene_id}'")

    # -- port conflicts ----------------------------------------------------------

    def _bound(self, service: str, port: int, why: Optional[str]) -> None:
        """Record a port that could not be bound (or that came free)."""
        if why:
            if service not in self.port_conflicts:
                holder = port_holder(port)
                self.port_conflicts[service] = {"port": port, "holder": holder, "reported": False, "why": why}
                log.error(f"[PORT] {port_conflict_message(service, port, holder)}")
        elif service in self.port_conflicts:
            info = self.port_conflicts.pop(service)
            log.info(f"[PORT] {service} is listening on port {port} again")
            if info.get("reported"):
                self._emit_edge_event("info", "port_conflict_cleared", f"{service} tcp port {port} is free again",
                                      {"service": service, "port": port, "protocol": "tcp"})
        self._update_port_fault()
        self._report_port_conflicts()

    def _update_port_fault(self) -> None:
        if not self.port_conflicts:
            self.faults.clear(F.PORT)
            return
        service, info = next(iter(self.port_conflicts.items()))
        msg = port_conflict_message(service, info["port"], info.get("holder"))
        extra = len(self.port_conflicts) - 1
        self.faults.set(F.PORT, msg + (f" (+{extra} more port)" if extra else ""))

    def _report_port_conflicts(self) -> None:
        if not self.player_id:
            return       # reported once the screen is enrolled
        for service, info in self.port_conflicts.items():
            if not info["reported"]:
                info["reported"] = True
                self._emit_edge_event("warn", "port_conflict",
                                      port_conflict_message(service, info["port"], info.get("holder")),
                                      {"service": service, "port": info["port"], "protocol": "tcp",
                                       "holder": info.get("holder"), "reason": info.get("why")})

    async def _port_retry_loop(self) -> None:
        """A port another program held may come free: try again every 30 s."""
        servers = {"local API": lambda: self.local_api, "control API": lambda: self.lan_api,
                   "renderer page": lambda: self.renderer}
        while not self._stop.is_set():
            await asyncio.sleep(PORT_RETRY_INTERVAL)
            for service in list(self.port_conflicts):
                srv = servers.get(service, lambda: None)()
                if srv is not None:
                    self._bound(service, srv.port, await asyncio.to_thread(srv.start))

    # -- NDI output -------------------------------------------------------------

    def _make_ndi_source(self):
        """Where the NDI frames come from: the whole X display on X11, the
        renderer page itself (Chromium screencast) on Wayland where an X grab
        is black, mpv's own picture in mpv-only mode on Wayland."""
        wayland = sys.platform.startswith("linux") and display.session_type() == "wayland"
        if wayland and self.page_mode and self.renderer is not None:
            return ndi_out.ScreencastSource(self.renderer.url)
        if wayland and self.mpv is not None:
            return ndi_out.MpvSource(self.mpv)
        return ndi_out.XGrabSource(self.settings.display)

    async def _set_cms_ndi(self, want: bool, name: Optional[str]) -> None:
        self._cms_ndi, self._cms_ndi_name = want, name
        await asyncio.to_thread(self._apply_ndi_output)

    def _apply_ndi_output(self):
        """Start, rename or stop the output by NdiOutputPolicy: the local setting
        or the CMS may turn it on; a CMS "off" stops only what the CMS started."""
        on, name = ndi_out.decide(self.settings.ndi_output, self.settings.ndi_output_name or ndi_out.default_name(),
                                  self._cms_ndi, self._cms_ndi_name)
        if not on:
            self.ndi_output.stop()
            self._ndi_refused = None
            return True, "not broadcasting"
        ok, msg = self.ndi_output.start(name)
        if not ok and self._ndi_refused != (on, name):
            log.warning(f"[NDI OUT] cannot broadcast as '{name}': {msg}")
        self._ndi_refused = None if ok else (on, name)
        return ok, msg

    # -- the control API on 8083 (its own thread → this loop) -----------------

    def _on_loop(self, coro, timeout: float = 15.0):
        if self._loop is None:
            raise RuntimeError("the player is not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _lan_wall(self, cmd: str, args: dict):
        """A wall route from a LAN sensor or controller → (accepted, reason, scene)."""
        if cmd == "patch":
            ok, msg = await self._template_patch(args)
        else:
            ok, msg = self._wall_command(cmd, args, source="lan")
        return ok, msg, self.wall.active_scene_id if self.wall else None

    def _lan_wall_threadsafe(self, cmd: str, args: dict):
        try:
            return self._on_loop(self._lan_wall(cmd, args))
        except Exception as e:
            return False, f"the player could not handle it ({type(e).__name__})", None

    async def _lan_snapshot(self) -> dict:
        if self.wall is None:
            tpl = self._template_on_screen()
            return {"interactive": False, "template_id": tpl.template_id if tpl else None,
                    "reason": "no interactive template on screen"}
        snap = self.wall.snapshot()
        snap["template_id"] = self._wall_item.template_id if self._wall_item else None
        return snap

    def _lan_snapshot_threadsafe(self) -> dict:
        try:
            return self._on_loop(self._lan_snapshot())
        except Exception:
            return {"interactive": False, "reason": "the player is not running"}

    async def _lan_control(self, name: str) -> None:
        if name == "play":
            await self.engine.play()
        elif name == "pause":
            self.engine.pause()
        elif name == "restart":
            await self.engine.replay()
            if self.wall is not None:
                self.wall.reset()

    def _lan_control_threadsafe(self, name: str) -> None:
        self._on_loop(self._lan_control(name))

    async def _wall_tick_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(0.5)
            if self.wall is not None:
                try:
                    self.wall.tick()
                except Exception as e:
                    log.debug(f"[WALL] tick failed: {e}")

    # -- template_patch -------------------------------------------------------

    async def _template_patch(self, args: dict):
        item = self._template_on_screen()
        if item is None:
            return False, "no template on screen"
        tid = args.get("template_id")
        if isinstance(tid, str) and tid and item.template_id and tid != item.template_id:
            return False, f"template {tid} is not on screen"
        eid = args.get("element_id")
        if not eid:
            return False, "template_patch needs 'element_id'"
        definition = item.extra.get("definition")
        if definition is None:
            # shown through the CMS render page: the page reloads with the change
            await self.surface.show_page(item, force=True)
            return True, f"patched {eid} (page reloaded)"
        ok, msg, new_def = patch_definition(definition, args)
        if not ok:
            return False, msg
        cid = args.get("content_id")
        kind = str(args.get("kind") or args.get("type") or "")
        if cid and kind in ("image", "video"):
            path = await self._ensure_media(str(cid), kind, network=True)
            if path is None:
                return False, f"content {cid} could not be fetched"
        built = await TemplateBuilder(self._nested_def_fetcher(True),
                                      lambda c, r: self._ensure_media(c, r, network=True)).build(item.template_id, new_def)
        if built is None:
            return False, f"kind {kind} not patchable"
        element = next((e for e in built["payload"]["elements"] if e.get("id") == eid), None)
        try:
            write_json_atomic(self.paths.cache_dir / f"template_{item.template_id}.json", new_def)
        except OSError:
            pass
        item.extra.update(definition=new_def, page=built["payload"], built=built,
                          deps=list(built["media"].values()))
        item.content_hash = built["hash"]
        self._rss_feeds |= built["rss"]
        self.surface.send({"type": "template_patch", "tid": item.template_id, "element": element,
                           "media": built["payload"]["media"]})
        return True, f"patched {eid}"

    # -- state commands ------------------------------------------------------

    async def _state_command(self, cmd: str, a: dict) -> None:
        if cmd == "load_playlist":
            incoming = a.get("playlist_hash") or a.get("playlist_id")
            if a.get("playlist_id"):
                self._playlist_uuid = str(a["playlist_id"])
            if incoming and incoming == self.last_playlist_hash:
                if self.engine.has_content:
                    return
                if time.monotonic() - self._last_empty_fetch < EMPTY_REFETCH_THROTTLE:
                    return
            self._last_empty_fetch = time.monotonic()
            if incoming != self.last_playlist_hash:
                log.info(f"[PLAYLIST] new playlist hash {incoming}")
            self.last_playlist_hash = incoming or None
            self.last_content_id = self._content_hash = None
            self._leave_walls()
            self.spawn_fetch()

        elif cmd == "load_content":
            cid = a.get("content_id")
            if not cid:
                return
            h = a.get("content_hash")
            if cid == self.last_content_id:
                if h and self._content_hash and h != self._content_hash:
                    log.info(f"[CONTENT] {cid} was edited — reloading")
                    self._content_hash = h
                    self._last_sent_key = None
                elif h and not self._content_hash:
                    self._content_hash = h
                    return
                else:
                    return
            else:
                self.last_content_id, self._content_hash = str(cid), h
                self.last_playlist_hash = None
            self._leave_walls()
            asyncio.ensure_future(self.load_single_content(str(cid), self._duration(a)))

        elif cmd == "load_wall_content":
            wid = a.get("wall_content_id") or a.get("content_id")
            if not wid:
                return
            key = f"{wid}_{json.dumps(a.get('wall_crop'), sort_keys=True)}"
            if key == self._wall_key:
                return
            self._cancel_wall()
            self._wall_key = key
            self.engine.set_crop(normalise_crop(a.get("wall_crop")))
            self.last_content_id, self.last_playlist_hash = str(wid), None
            log.info(f"[WALL] content {wid} crop={a.get('wall_crop')}")
            asyncio.ensure_future(self.load_single_content(str(wid), self._duration(a)))

        elif cmd == "load_wall_rtp":
            url = a.get("rtp_url")
            if not url:
                return
            crop = a.get("wall_crop")
            key = f"rtp_{url}_{json.dumps(crop, sort_keys=True)}"
            if key == self._ndi_active_key and self._wall_task and not self._wall_task.done():
                mpv = self._stream_mpv(create=False)
                if mpv is None or not mpv.core_idle:
                    return
                log.info("[WALL STREAM] same stream but mpv idle — reopening")
            self._cancel_wall()
            self._ndi_active_key = key
            self._wall_task = asyncio.ensure_future(self._play_wall_rtp(url, crop))

        elif cmd == "stop_wall":
            log.info("[WALL] stop_wall — back to the playlist")
            self._leave_walls()
            await self.engine.resume()

        elif cmd == "show_capture":
            await self._show_capture(a)

        elif cmd == "hide_capture":
            if self._live_key:
                self._live_key = None
                await self.engine.resume()

        elif cmd == "show_ndi":
            name = a.get("source_name") or a.get("ndi_source_name")
            if name and self._ndi_receiver:
                key = f"ndi_{name}"
                if key == self._ndi_active_key:
                    return
                self._ndi_active_key = key
                await self.engine.suspend()
                await self.surface.stop()
                await asyncio.to_thread(self._ndi_receiver.start, name)

        elif cmd == "hide_ndi":
            if self._ndi_receiver:
                self._ndi_receiver.stop()
            self._ndi_active_key = None
            await self.engine.resume()

        elif cmd == "show_ndi_wall":
            name = a.get("source_name") or a.get("ndi_source_name")
            crop = a.get("wall_crop")
            if name and self._ndi_receiver:
                key = f"ndi_{name}_{json.dumps(crop, sort_keys=True)}"
                if key == self._ndi_active_key:
                    return
                self._ndi_active_key = key
                await self.engine.suspend()
                await self.surface.stop()
                n = normalise_crop(crop)
                ndi_crop = {"x": n[0], "y": n[1], "w": n[2], "h": n[3]} if n else None
                await asyncio.to_thread(self._ndi_receiver.start, name, ndi_crop)

        elif cmd == "start_ndi_broadcast":
            await self._set_cms_ndi(True, a.get("ndi_broadcast_name") or a.get("name"))

        elif cmd == "stop_ndi_broadcast":
            await self._set_cms_ndi(False, None)

    @staticmethod
    def _duration(a: dict) -> float:
        try:
            return max(0.0, float(a.get("duration") or 0))
        except (TypeError, ValueError):
            return 0.0

    async def _show_capture(self, a: dict) -> None:
        kind = str(a.get("capture_kind") or "").lower()
        label = a.get("capture_device_label")
        device_id = a.get("capture_device_id")
        audio = try_read_bool(a.get("audio_enabled"))
        if audio is None:
            audio = try_read_bool(a.get("emit_audio"))
        decklink = kind in ("decklink", "blackmagic") or str(device_id or "").startswith("decklink:")
        if audio is None:
            audio = decklink        # DeckLink audio on, USB off (DVSI defaults)
        key = f"capture:{device_id}:{label}:a{int(bool(audio))}"
        trim = av_offset(a)
        if key == self._live_key:
            # the trim is not part of the key: a new value is applied live
            if trim != self._live_trim:
                self._live_trim = trim
                self.surface.send({"type": "capture_trim", "av_offset_ms": trim})
            return
        if decklink:
            log.warning("[CAPTURE] DeckLink capture is not supported on Linux — trying it as a USB device")
        if not self.page_mode:
            log.warning("[CAPTURE] live capture needs the renderer page (Chromium); not shown")
            return
        self._live_key = key
        self._cancel_wall()
        self._wall_key = self._ndi_active_key = None
        await self.engine.suspend()
        log.info(f"[CAPTURE] showing {label or device_id or 'the first camera'} (audio {'on' if audio else 'off'})")
        self._live_trim = trim
        self.surface.send({"type": "capture", "gen": self.surface.gen + 1, "device_label": label,
                           "device_id": None if decklink else device_id, "audio": bool(audio),
                           "av_offset_ms": trim})

    def _cancel_wall(self) -> None:
        if self._wall_task and not self._wall_task.done():
            self._wall_task.cancel()
        self._wall_task = None

    def _leave_walls(self) -> None:
        """A playlist or direct pick takes the screen back from any wall or live input."""
        self._cancel_wall()
        self._wall_key = None
        self._ndi_active_key = None
        self._live_key = None
        if self._ndi_receiver and self._ndi_receiver.active:
            self._ndi_receiver.stop()
        self.engine.set_crop(None)

    def _stream_mpv(self, create: bool = True):
        """mpv for the multicast wall: the main mpv in mpv mode, a dedicated
        one (created on demand, closed after) in renderer-page mode."""
        if not self.page_mode:
            return self.mpv
        if self._wall_mpv is None and create:
            from .playback import MpvPlayer
            self._wall_mpv = MpvPlayer(self.settings.display, self.settings.audio_output)
            self._wall_mpv.initialise(asyncio.get_running_loop())
            self._wall_mpv.set_volume(self.volume, self.muted)
        return self._wall_mpv

    async def _play_wall_rtp(self, url: str, crop: Optional[dict]) -> None:
        """Server-streamed wall: open the multicast stream and crop this
        display's region. If mpv goes idle (the server restarted ffmpeg), the
        key is cleared so the next heartbeat re-opens it."""
        vf = crop_filter_pixel(crop)
        log.info(f"[WALL STREAM] Opening {url} vf={vf!r}")
        await self.engine.suspend()
        try:
            mpv = self._stream_mpv()
        except Exception as e:
            log.error(f"[WALL STREAM] mpv unavailable: {e}")
            return
        if not self.page_mode:
            mpv.stop()
        try:
            await asyncio.sleep(0.3)
            for k, v in STREAM_TUNING.items():
                mpv.set(k, v)
            mpv.play_file(stream_url_with_buffers(url), vf=vf)
            await asyncio.sleep(5)
            while True:
                await asyncio.sleep(2)
                if mpv.core_idle:
                    log.warning("[WALL STREAM] Stream died — clearing key for auto-recovery")
                    self._ndi_active_key = None
                    return
        except asyncio.CancelledError:
            log.info("[WALL STREAM] stopped (switching content)")
        finally:
            if self.page_mode and self._wall_mpv is not None:
                self._wall_mpv.terminate()
                self._wall_mpv = None
            else:
                mpv.set("keep-open", "always")
                mpv.set("cache", "auto")
                mpv.set("profile", "default")

    # ------------------------------------------------------------------
    # Page events (renderer → player)
    # ------------------------------------------------------------------

    def _page_event_threadsafe(self, msg: dict) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._page_event, msg)

    def _page_event(self, msg: dict) -> None:
        t = msg.get("type")
        if self.page_mode:
            self.surface.on_page_event(msg)
        if t == "alive" and self.kiosk is not None:
            self.kiosk.alive()
        elif t == "setup":
            asyncio.ensure_future(self._on_setup(msg))
        elif t == "reset_registration":
            asyncio.ensure_future(self.reset_registration("screen"))
        elif t == "tap" and self.wall is not None and self.wall.m.policy.touch:
            tid = try_read_int(msg.get("trigger_id"))
            if tid is not None:
                ok, why = self.wall.trigger(tid, "touch")
                log.info(f"[WALL] tap {tid}: {why}")
        elif t == "zone_playing" and self.wall_host is not None:
            self.wall_host.page_playing(str(msg.get("surface") or ""))
        elif t == "zone_returned" and self.wall_host is not None:
            self.wall_host.page_returned(str(msg.get("surface") or ""))
        elif t == "devices":
            devs = msg.get("capture") if isinstance(msg.get("capture"), list) else []
            self.capture_devices = [{"label": d.get("label"), "deviceId": d.get("deviceId")}
                                    for d in devs if isinstance(d, dict)]
        elif t == "capture_trim":
            self.av_offset_ms = try_read_int(msg.get("av_offset_ms"))
            self.av_hold_ms = try_read_int(msg.get("av_hold_ms"))
        elif t == "error":
            log.warning(f"[RENDERER] {msg.get('what')}")

    def _renderer_recovered(self, info: dict) -> None:
        if not self.player_id:
            return
        reason = info.get("reason")
        self._emit_edge_event("error", "renderer_crash", f"Chromium renderer {info.get('kind')} ({reason}), recovery: relaunch",
                              {**info, "outcome": "relaunched"})

    # ------------------------------------------------------------------
    # Playlist fetch / apply
    # ------------------------------------------------------------------

    def spawn_fetch(self) -> None:
        if self._fetch_task and not self._fetch_task.done():
            return
        self._fetch_task = asyncio.ensure_future(self.fetch_playlist())

    async def fetch_playlist(self, force: bool = False) -> bool:
        """GET the assigned playlist and apply it; True when one was applied."""
        if not self.player_id:
            return False
        async with self._fetch_lock:
            try:
                data = await self.cms.assigned_playlist(self.player_id)
            except Exception as e:
                self._fetch_failures += 1
                log.error(f"Failed to fetch playlist: {e}")
                if self._fetch_failures >= PLAYLIST_FETCH_FAULT_AFTER:
                    self.faults.set(F.PLAYLIST_FETCH, F.playlist_fetch_message(
                        self._fetch_failures, type(e).__name__, self.paths.offline_playlist.exists()))
                if not self.engine.has_content:
                    await self._play_offline()
                return False
            self._fetch_failures = 0
            self.faults.clear(F.PLAYLIST_FETCH)
            if not data or not data.get("items"):
                log.info("No playlist assigned to this player on the CMS.")
                return False
            return await self.process_playlist(data, force=force)

    async def _play_offline(self) -> bool:
        try:
            data = json.loads(self.paths.offline_playlist.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or not data.get("items"):
            return False
        log.info("[OFFLINE] Playing the saved playlist")
        # Cache only: at boot this runs before the CMS has been reached, and
        # must not wait on downloads or lookups. The next fetch fills gaps.
        return await self.process_playlist(data, prune=False, network=False)

    def _offline_record(self, content_id: str) -> Optional[dict]:
        try:
            data = json.loads(self.paths.offline_playlist.read_text(encoding="utf-8"))
            for it in data.get("items") or []:
                if str(it.get("content_id")) == content_id and isinstance(it.get("content"), dict):
                    return it["content"]
        except (OSError, ValueError, AttributeError):
            pass
        return None

    async def _content_meta(self, content_id: str) -> Optional[dict]:
        """Content metadata, waiting out CM:EX's 429s within a small budget."""
        spent = 0.0
        for _attempt in range(4):
            try:
                return await self.cms.content_meta(content_id)
            except RateLimited as e:
                wait = min(5.0, max(1.0, e.retry_after or 1.0))
                if spent + wait > 30:
                    return None
                spent += wait
                await asyncio.sleep(wait)
            except Exception as e:
                log.warning(f"[CONTENT] metadata for {content_id} unavailable: {e}")
                return None
        return None

    async def load_single_content(self, content_id: str, duration: float = 0.0, force: bool = False) -> bool:
        """A content pushed directly (not in a playlist): fetch its full
        metadata so a template/URL/RSS is classified like a playlist item."""
        meta = await self._content_meta(content_id) or self._offline_record(content_id)
        if not meta:
            log.error(f"[CONTENT] {content_id}: no metadata and no offline record — leaving the screen as is")
            self.last_content_id = None
            return False
        mock = {"items": [{"content_id": content_id, "content": meta, "duration": duration}]}
        return await self.process_playlist(mock, force=force, prune=False)

    def _resolve(self, item: Item) -> Optional[Path]:
        if item.path is None:
            return None
        return self.cache.find(item.content_id, item.path)

    # -- templates ------------------------------------------------------------

    def _nested_def_fetcher(self, network: bool):
        async def fetch(tid: str) -> Optional[dict]:
            return await self._template_definition(tid, network)
        return fetch

    async def _template_definition(self, tid: str, network: bool) -> Optional[dict]:
        path = self.paths.cache_dir / f"template_{tid}.json"
        if network and self.player_id:
            d = await self.cms.template_definition(self.player_id, tid)
            if d:
                try:
                    write_json_atomic(path, d)
                except OSError:
                    pass
                return d
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    async def _ensure_media(self, cid: str, role: str, network: bool) -> Optional[Path]:
        """A template / wall media file, cached as {content_id}{ext}. Returns
        the path even while its download is pending, so the template lists it
        and shows it once the file lands."""
        found = self.cache.find(cid, self.cache.dir / f"{cid}{'.png' if role == 'image' else '.mp4'}")
        if found is not None:
            return found
        if not network:
            return None
        meta = await self._content_meta(cid)
        filename = (meta or {}).get("filename") or ""
        ext = Path(filename).suffix.lower() or (".png" if role == "image" else ".mp4")
        dest = self.cache.dir / f"{cid}{ext}"
        got = await self._download(cid, dest, filename or dest.name)
        return got or dest

    async def _prepare_template(self, item: Item, kept: set, network: bool) -> None:
        if not item.template_id:
            return
        kept.add(f"template_{item.template_id}.json")
        definition = await self._template_definition(item.template_id, network)
        if not definition:
            return
        try:
            item.canvas = (int(definition.get("width") or 1920), int(definition.get("height") or 1080))
        except (TypeError, ValueError):
            pass
        if not self.surface.supports_native:
            return
        built = await TemplateBuilder(self._nested_def_fetcher(network),
                                      lambda c, r: self._ensure_media(c, r, network)).build(item.template_id, definition)
        if built is None:
            return
        item.extra.update(definition=definition, page=built["payload"], built=built,
                          deps=list(built["media"].values()))
        item.content_hash = built["hash"]
        kept |= built["files"]
        self._rss_feeds |= built["rss"]

    # -- web pages ----------------------------------------------------------------

    async def _fetch_snapshot(self, item: Item) -> None:
        """The page's saved still, if the CMS has one (409 = live only)."""
        dest = self.cache.dir / f"{item.content_id}.snapshot.png"
        try:
            await self.cms.download(item.content_id, dest)
        except Exception:
            pass

    async def _web_probe(self, item: Item) -> dict:
        """How to show a web item: in the page, in its own window (the site
        refuses to be framed), or as its saved still (unreachable)."""
        snap = self.cache.dir / f"{item.content_id}.snapshot.png"
        extra = {}
        if snap.exists():
            when = datetime.fromtimestamp(snap.stat().st_mtime).strftime("%-d %b %H:%M") \
                if sys.platform != "win32" else datetime.fromtimestamp(snap.stat().st_mtime).strftime("%d %b %H:%M")
            extra.update(snapshot=f"/media/{snap.name}", badge=f"Saved page · {when}")
        if self.online is False and snap.exists():
            return {**extra, "snapshot_only": True}
        url = item.url or ""
        if url.startswith(self.cms_url):
            return extra     # CM:EX's own pages frame fine
        cached = self._web_probe_cache.get(url)
        if cached and time.monotonic() - cached[0] < WEB_PROBE_TTL:
            return {**extra, **cached[1]}
        verdict = {}
        try:
            async with httpx.AsyncClient(timeout=6, follow_redirects=True) as c:
                async with c.stream("GET", url) as r:
                    xfo = (r.headers.get("x-frame-options") or "").lower()
                    csp = (r.headers.get("content-security-policy") or "").lower()
                    fa = next((d for d in csp.split(";") if d.strip().startswith("frame-ancestors")), "")
                    if xfo in ("deny", "sameorigin") or (fa and "*" not in fa):
                        verdict = {"window": True}
        except Exception:
            if snap.exists():
                verdict = {"snapshot_only": True}
        self._web_probe_cache[url] = (time.monotonic(), verdict)
        return {**extra, **verdict}

    # -- apply ----------------------------------------------------------------

    async def process_playlist(self, data: dict, force: bool = False, prune: bool = True,
                               network: bool = True) -> bool:
        """Save the offline copy, classify, prepare templates, download what is
        missing (playing as soon as the first item is ready), then
        retain/prune the cache."""
        raw = data.get("items") or []
        if not raw:
            return False
        try:
            write_json_atomic(self.paths.offline_playlist, data)
        except OSError as e:
            log.error(f"Failed to save offline playlist: {e}")

        final: List[Item] = []
        kept: set = set()
        to_download: List[Item] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            item = classify(entry, self.cms, self.cache.dir)
            if item is None:
                continue
            if item.kind == TEMPLATE:
                await self._prepare_template(item, kept, network)
            elif item.kind == WEB and network and "/rss/" not in (item.url or ""):
                kept.add(f"{item.content_id}.snapshot.png")
                asyncio.ensure_future(self._fetch_snapshot(item))
            elif item.kind == WEB:
                kept.add(f"{item.content_id}.snapshot.png")
            if item.is_media:
                found = self.cache.find(item.content_id, item.path)
                if found is not None:
                    item.path = found
                kept.add(item.path.name)
                if force or found is None:
                    to_download.append(item)
            final.append(item)
        if not final:
            return False

        self.pending.keep_only({it.content_id for it in final if it.is_media} |
                               {p.content_id for p in self.pending.entries.values()
                                if any(p.path in it.extra.get("deps", []) for it in final)})
        key = "|".join(f"{it.path or it.url}:{it.duration:g}:{it.content_hash}" for it in final)

        if not network:
            to_download = []
        if to_download and self.cache.is_full():
            log.error("Cache is full (50 GB) — refusing new downloads")
            self.faults.set(F.CACHE_FULL, F.cache_full_message(50))
            to_download = []
        else:
            self.faults.clear(F.CACHE_FULL)

        if not to_download:
            if key != self._last_sent_key or not self.engine.has_content:
                await self.engine.load(final)
            self._last_sent_key = key
        else:
            total = len(to_download)
            log.info(f"Need to download {total} file(s); playback starts once the first item is ready.")
            started = False
            if not force and self.engine.ready(final[0]):
                await self.engine.load(final)
                started = True
            for i, item in enumerate(to_download):
                log.info(f"[DOWNLOADING {i + 1}/{total}] {item.name}")
                self.surface.download_progress(i, total, 0.0)

                def progress(done, size, i=i):
                    if size:
                        self.surface.download_progress(i, total, done / size)

                got = await self._download(item.content_id, item.path, item.name, progress)
                if got is not None:
                    item.path = got
                if got is not None and not started and self.engine.ready(final[0]):
                    await self.engine.load(final)
                    started = True
                elif got is not None:
                    await self.engine.kick()
            self.surface.download_progress(total, total, 1.0)
            await self.engine.load(final)
            self._last_sent_key = key

        if prune:
            self.cache.retain_and_prune(kept)
        else:
            self.cache.enforce_lru(kept)
        return True

    async def _download(self, cid: str, path: Path, name: str, progress=None) -> Optional[Path]:
        lock = self._dl_locks.setdefault(str(path), asyncio.Lock())
        async with lock:
            try:
                got = await self.cms.download(cid, path, progress)
            except ContentNotReady as e:
                log.info(f"[DOWNLOAD] {name} not ready yet (retry in {e.retry_after or 30:.0f}s)")
                self.pending.not_ready(cid, path, name, e.retry_after)
                return None
            except ContentGone as e:
                log.error(f"[DOWNLOAD] {name} no longer exists ({e}) — not retrying")
                self._gone.add(cid)
                self.pending.done(cid)
                return None
            except (CmsError, httpx.HTTPError, OSError) as e:
                log.error(f"[DOWNLOAD] {name} failed: {e}")
                self.pending.failed(cid, path, name)
                await asyncio.sleep(2)
                return None
            got = repair_name(got)
            self.pending.done(cid)
            self._gone.discard(cid)
            log.info(f"[SUCCESS] Downloaded {got.name}")
            return got

    async def _pending_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(PENDING_INTERVAL)
            if not self.player_id or self.online is False:
                continue
            for p in self.pending.due():
                if await self._download(p.content_id, p.path, p.name) is not None:
                    await self.engine.kick()

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    async def refresh(self):
        """Release the players, force a re-download, and report the real
        outcome. Repeat deliveries are ignored while one runs."""
        self._refreshing = True
        try:
            log.info("[REFRESH] releasing players and re-downloading")
            self.last_playlist_hash = None
            self._last_sent_key = None
            direct = self.last_content_id
            await self.engine.release()
            await asyncio.sleep(0.4)
            if direct:
                applied = await self.load_single_content(direct, force=True)
            else:
                applied = await self.fetch_playlist(force=True)
            if not applied:
                restored = await self._play_offline()
                return False, ("no playlist came back — restored the cached playlist" if restored
                               else "no playlist came back")
            return True, "Refresh completed"
        finally:
            self._refreshing = False

    # ------------------------------------------------------------------
    # Proof of play, telemetry
    # ------------------------------------------------------------------

    def _on_item(self, new: Optional[Item], prev: Optional[Item], reason: str) -> None:
        """Item boundaries: proof of play (edge mode) and the wall runtime."""
        now = (_utc_now(), time.monotonic())
        if prev is not None and self._play_started is not None and self.edge_mode and prev.content_id:
            started, mono = self._play_started
            seconds = now[1] - mono
            if seconds >= PROOF_MIN_SECONDS and self.player_id:
                event = {"event_id": str(uuid.uuid4()), "type": "proof_of_play", "player_uuid": self.player_id,
                         "ts": ts(now[0]), "asset_uuid": prev.content_id, "started_at": ts(started),
                         "ended_at": ts(now[0]), "completed": reason == "completed",
                         "duration_ms": int(seconds * 1000)}
                if self._playlist_uuid:
                    event["playlist_uuid"] = self._playlist_uuid
                self.telemetry.enqueue(event)
        self._play_started = now if new is not None else None
        self._attach_wall(new)

    def _telemetry_dropped(self, event: dict) -> None:
        if event.get("type") == "inventory":
            log.warning("[INVENTORY] the cloud refused the inventory — reporting daily only from now on")
            self.inventory.refused((event.get("agent") or {}).get("version"))

    def _emit_edge_event(self, level: str, code: str, message: str, detail: dict) -> None:
        if self.player_id:
            self.telemetry.enqueue(edge_event(self.player_id, level, code, message, detail))
            self._telemetry_kick.set()

    def _edge_transition(self, code: str, cls: str, reason: str) -> None:
        self._emit_edge_event("warn" if code == "edge_failover" else "info", code,
                              f"{cls} {'failed over to the cloud' if code == 'edge_failover' else 'back on the edge'}: {reason}",
                              {"class": cls, "reason": reason})

    def _queue_stability_events(self) -> None:
        pid = self.player_id
        if not pid:
            return
        markers = pending_crash_markers(self.paths.crash_dir)
        for path in markers:
            try:
                marker = json.loads(path.read_text(encoding="utf-8"))
                self.telemetry.enqueue(crash_event(pid, marker))
                path.unlink()
            except (OSError, ValueError) as e:
                log.warning(f"[TELEMETRY] crash marker {path.name} unreadable: {e}")
        if not self._boot_reported:
            self.telemetry.enqueue(boot_event(pid, VERSION, self.started_at, bool(markers)))
            self._boot_reported = True
        self._report_port_conflicts()
        self._telemetry_kick.set()

    async def _flush_telemetry(self) -> None:
        pid = self.player_id
        if pid:
            await self.telemetry.flush(lambda events: self.cms.telemetry(pid, events))

    async def _telemetry_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._telemetry_kick.wait(), TELEMETRY_INTERVAL)
            except asyncio.TimeoutError:
                pass
            self._telemetry_kick.clear()
            if self.online is False and not self.edge_mode:
                continue
            try:
                await self._flush_telemetry()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug(f"[TELEMETRY] flush failed: {e}")

    def health_event(self) -> dict:
        """player_health: what keeps the screen 'online' in the cloud while its
        heartbeat goes to the edge. Unknown values are omitted, not zeroed;
        `error` is always present (null clears it upstream)."""
        m = metrics()
        e = {"event_id": str(uuid.uuid4()), "type": "player_health", "player_uuid": self.player_id, "ts": ts(),
             "status": self.engine.status()["status"], "player_version": VERSION,
             "uptime_s": int(time.time() - self._started_wall), "error": self.compute_error()}
        for src, dst in (("cpu_percent", "cpu_pct"), ("memory_percent", "mem_pct"),
                         ("disk_percent", "disk_pct"), ("cpu_temp", "temp_c")):
            if m.get(src) is not None:
                e[dst] = m[src]
        return e

    async def _health_loop(self) -> None:
        while not self._stop.is_set():
            if self.player_id:
                self.telemetry.enqueue(self.health_event())
                self._telemetry_kick.set()
            await asyncio.sleep(HEALTH_INTERVAL)

    async def _edge_probe_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(EDGE_PROBE_INTERVAL)
            if self.cms.router.any_failed_over:
                healthy = await self.cms.probe_edge()
                self.cms.router.probe_result(healthy)
                if healthy:
                    self._telemetry_kick.set()

    async def _inventory_loop(self) -> None:
        await asyncio.sleep(inv.FIRST_DELAY)
        while not self._stop.is_set():
            pid = self.player_id
            if pid and self.state.get("player_secret"):
                try:
                    body = await asyncio.to_thread(inv.collect, VERSION, self.local_ip)
                    if self.inventory.due(body):
                        event = dict(body, player_uuid=pid, event_id=str(uuid.uuid4()),
                                     ts=_utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"))
                        self.telemetry.events = [e for e in self.telemetry.events if e.get("type") != "inventory"]
                        self.telemetry.enqueue(event)
                        self.inventory.sent(body)
                        self._telemetry_kick.set()
                        log.info(f"[INVENTORY] queued ({body['apps_count']} apps, "
                                 f"{len(body['remote_access'])} remote-access tools)")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug(f"[INVENTORY] failed: {e}")
            await asyncio.sleep(inv.CHECK_EVERY)

    # ------------------------------------------------------------------
    # SSE, RSS, info, network changes
    # ------------------------------------------------------------------

    async def _sse_loop(self) -> None:
        while not self._stop.is_set():
            pid = self.player_id
            if pid and self.online is not False:
                try:
                    async for data in self.cms.events(pid):
                        await self.handle_response(data, channel="push")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug(f"[SSE] stream ended: {e}")
            await asyncio.sleep(SSE_RECONNECT)

    async def _rss_loop(self) -> None:
        await asyncio.sleep(2)
        while not self._stop.is_set():
            if self.online is not False and self.player_id:
                for cid in list(self._rss_feeds):
                    try:
                        data = await self.cms.rss_items(cid)
                    except Exception:
                        data = None
                    if data and data.get("items"):      # an empty payload never overwrites a good cache
                        try:
                            write_json_atomic(self.paths.rss_dir / f"{cid}.json", data)
                        except OSError:
                            pass
            try:
                await asyncio.wait_for(self._rss_wake.wait(), RSS_ONLINE if self.online is not False else RSS_OFFLINE)
            except asyncio.TimeoutError:
                pass
            self._rss_wake.clear()

    async def _info_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.local_ip = await asyncio.to_thread(
                    lan_address_for, self.edge_url if self.edge_mode else self.cms_url)
                if _ndi_available():
                    self.ndi_sources = await asyncio.to_thread(ndi_engine.find_sources, timeout_ms=2000)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug(f"[INFO] refresh failed: {e}")
            await asyncio.sleep(INFO_INTERVAL)

    async def _net_watch_loop(self) -> None:
        """Instant reconnect: while offline, an address change (the network
        came up, a cable went in) retries the CMS at once instead of waiting
        out the 15 s reconnect poll."""
        last = address_signature()
        while not self._stop.is_set():
            await asyncio.sleep(NET_WATCH_INTERVAL)
            sig = address_signature()
            if sig != last:
                last = sig
                if self.online is False:
                    log.info("[NET] network changed while offline — retrying the CMS now")
                    self._wake.set()

    # ------------------------------------------------------------------
    # Preview / local API helpers (run on worker threads)
    # ------------------------------------------------------------------

    def _grab_image(self):
        try:
            from PIL import Image, ImageGrab
        except ImportError:
            return None
        window_up = self.page_mode and self.surface.window is not None and self.surface.window.running
        stream_up = self._wall_task is not None and not self._wall_task.done()
        if self.page_mode and self.renderer is not None and not window_up and not stream_up:
            # straight from Chromium: works where an X grab is black (XWayland) or refused
            jpeg = cdp.capture(self.renderer.url)
            if jpeg:
                try:
                    img = Image.open(io.BytesIO(jpeg))
                    img.load()
                    return img
                except OSError:
                    pass
        if self.page_mode or (self.mpv is None) or getattr(getattr(self.surface, "browser", None), "running", False):
            try:
                return ImageGrab.grab(xdisplay=self.settings.display)
            except Exception as e:
                log.debug(f"[PREVIEW] screen grab failed: {e}")
                return None
        tmp = self.paths.preview_tmp.with_suffix(".png")
        if self.mpv.screenshot(tmp):
            try:
                img = Image.open(tmp)
                img.load()
                return img
            except OSError:
                return None
        return None

    async def _capture_frame(self):
        return await asyncio.to_thread(self._grab_image)

    async def _post_frame(self, jpeg: bytes) -> Optional[bool]:
        if not self.player_id:
            return False
        return await self.cms.preview_frame(self.player_id, jpeg)

    def _snapshot_jpeg(self) -> Optional[bytes]:
        img = self._grab_image()
        return encode_jpeg(img, (640, 360), 50) if img is not None else None

    def _device_info(self) -> dict:
        return {
            "ip_address": self.local_ip,
            "platform": "linux",
            "player_version": VERSION,
            "renderer": "chromium" if self.page_mode else "mpv",
            "ndi_available": _ndi_available(),
            "preview_supported": True,
        }

    def _ndi_source_list(self) -> list:
        if not _ndi_available():
            return []
        return ndi_engine.find_sources(timeout_ms=3000)


def _suppress_desktop_popups() -> None:
    """Kill notification daemons and update notifiers so nothing pops up
    over the fullscreen content (signage machines do not need them)."""
    if not sys.platform.startswith("linux"):
        return
    names = ["dunst", "notify-osd", "xfce4-notifyd", "mako", "swaync",
             "mate-notification-daemon", "lxqt-notificationd", "cinnamon-notificationd",
             "update-notifier", "update-manager", "gnome-software", "packageupdater", "mintupdate"]
    for name in names:
        try:
            subprocess.run(["pkill", "-x", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    try:
        subprocess.Popen(["xdg-screensaver", "reset"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    log.info("Desktop popups suppressed")

"""
The player orchestrator: enrollment, heartbeat, SSE push, command handling,
playlist download/apply, refresh and re-enrollment.

Behaviour mirrors DVSI Plus `Cmx/CmxPlayerService.cs` (cloud mode) — see
README.md "How it works" for the summary, and the module docstrings of
cms_client, commands, cache, playback and telemetry for the detailed rules.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import httpx

from . import VERSION
from . import faults as F
from . import screens
from .browser import Browser
from .cache import CacheStore, PendingDownloads, repair_name
from .cms_client import CmsClient, CmsError, ContentGone, ContentNotReady, RateLimited
from .commands import (ENVELOPE_COMMANDS, RESEND, RUNNING, STATE_COMMANDS, CommandGate,
                       Coalescer, not_supported, parse_seek, parse_volume, try_read_bool)
from .config import Paths, Settings, load_state, save_state, write_json_atomic
from .enrollment import CODE_POLL_CAP, NEW_CODE_RETRY, POLL_INTERVAL, enroll_link, poll_wait
from .health import lan_address_for, metrics
from .items import TEMPLATE, VIDEO, Item, classify
from .local_api import PORT as LOCAL_API_PORT, LocalApiServer
from .playback import MpvPlayer, PlaylistEngine
from .preview import PreviewPusher, encode_jpeg
from .telemetry import (TelemetryQueue, boot_event, crash_event, pending_crash_markers)
from .wall import (STREAM_TUNING, crop_filter_normalised, crop_filter_pixel, normalise_crop,
                   stream_url_with_buffers)

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
INFO_INTERVAL = 30.0
EMPTY_REFETCH_THROTTLE = 3.0
OFFLINE_NOTICE_SECONDS = 6.0
COMMAND_LOG_SUMMARY = 30.0
PLAYLIST_FETCH_FAULT_AFTER = 3


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


class Player:
    def __init__(self, settings: Settings, paths: Paths, *,
                 transport: Optional[httpx.AsyncBaseTransport] = None,
                 mpv=None, browser=None, enable_local_api: bool = True):
        self.settings = settings
        self.paths = paths
        paths.ensure()
        self.state: dict = load_state(paths)
        self._adopt_cms_url(settings.cms_url)

        self.cms = CmsClient(settings.cms_url, lambda: self.state.get("player_secret"), transport)
        self.mpv = mpv if mpv is not None else MpvPlayer(settings.display, settings.audio_output)
        self.browser = browser if browser is not None else Browser(settings.display, paths.browser_profile)
        self.cache = CacheStore(paths.cache_dir, paths.cache_history, settings.keep_playlists)
        self.pending = PendingDownloads()
        self.faults = F.PlayerFaults()
        self.gate = CommandGate()
        self.coalescer = Coalescer()
        self.telemetry = TelemetryQueue(paths.telemetry_queue)
        self.engine = PlaylistEngine(
            self.mpv, self.browser,
            default_image=settings.default_image_duration,
            resolve=self._resolve,
            blank=self._show_black,
            on_waiting=self._show_waiting,
            screen_size=self.mpv.display_size,
            crop_filter=crop_filter_normalised,
        )
        self.preview = PreviewPusher(self._capture_frame, self._post_frame)
        self._enable_local_api = enable_local_api
        self.local_api: Optional[LocalApiServer] = None

        self.volume = int(self.state.get("volume", 100))
        self.muted = bool(self.state.get("muted", False))

        # Dedup / command state
        self.last_playlist_hash: Optional[str] = None
        self.last_content_id: Optional[str] = None
        self._content_hash: Optional[str] = None
        self._last_sent_key: Optional[str] = None
        self._last_empty_fetch = 0.0
        self._wall_key: Optional[str] = None
        self._ndi_active_key: Optional[str] = None
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

        # Lifecycle
        self.online: Optional[bool] = None
        self.exit_code = 0
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._telemetry_kick = asyncio.Event()
        self._routines: Dict[str, asyncio.Task] = {}
        self._enroll_task: Optional[asyncio.Task] = None
        self._wall_task: Optional[asyncio.Task] = None
        self._offline_notice: Optional[asyncio.TimerHandle] = None
        self._boot_reported = False
        self.started_at = datetime.now(timezone.utc)
        self._started_wall = time.time()
        self.local_ip: Optional[str] = None
        self.ndi_sources: List[dict] = []
        self._ndi_receiver = ndi_engine.NDIReceiver() if ndi_engine else None
        self._ndi_sender = ndi_engine.NDISender() if ndi_engine else None

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def player_id(self) -> Optional[str]:
        return self.state.get("player_id")

    def _adopt_cms_url(self, url: str) -> None:
        """A different CMS host means a different registration."""
        if not url:
            return
        saved = self.state.get("cms_url")
        if saved and _host_port(saved) != _host_port(url) and self.player_id:
            log.warning(f"CMS changed ({saved} → {url}) — this screen will register again")
            for k in ("player_id", "player_secret", "site_id", "site_name"):
                self.state.pop(k, None)
        self.state["cms_url"] = url
        save_state(self.paths, self.state)

    def _save(self) -> None:
        save_state(self.paths, self.state)

    # ------------------------------------------------------------------
    # Screens
    # ------------------------------------------------------------------

    def _screen(self, name: str, render) -> None:
        path = self.paths.screens_dir / f"{name}.png"
        try:
            ok = render(path)
        except Exception as e:
            log.warning(f"[SCREEN] {name} failed: {e}")
            ok = False
        if ok:
            self.mpv.clear_osd()
            self.mpv.show_still(str(path))

    def _show_black(self) -> None:
        self._screen("black", screens.render_black)

    def _show_message(self, name: str, title: str, body: str = "") -> None:
        if screens.available():
            self._screen(name, lambda p: screens.render_message(p, self.mpv.display_size(), title, body))
        else:
            self._show_black()
            self.mpv.show_osd(f"{title}\n\n{body}".strip(), 3_600_000)

    def _show_waiting(self) -> None:
        self._show_message("waiting", "CMX Player — waiting for content…")

    def _show_enrollment(self, code: str) -> None:
        link = enroll_link(self.settings.cms_url, code, self.state.get("site_id"))
        server = self.settings.cms_url
        site = self.state.get("site_name")
        if screens.available():
            self._screen("enrollment", lambda p: screens.render_enrollment(
                p, self.mpv.display_size(), code, server, site, link))
        else:
            self._show_black()
            self.mpv.show_osd(f"REGISTRATION CODE\n\n  {code}  \n\n"
                              "Enter this code in CM:EX to approve this player.", 3_600_000)

    # ------------------------------------------------------------------
    # Run / shutdown
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError, ValueError):
                pass

        if not os.environ.get("DISPLAY"):
            os.environ["DISPLAY"] = self.settings.display
        _suppress_desktop_popups()

        if self._enable_local_api:
            self.local_api = LocalApiServer(self.paths.local_api_key, self._device_info,
                                            self._ndi_source_list, self._snapshot_jpeg)
            why = self.local_api.start()
            self.faults.put(F.PORT, F.port_message(LOCAL_API_PORT, why) if why else None)

        if hasattr(self.mpv, "initialise"):
            self.mpv.initialise(loop)
        self.mpv.set_eof_callback(self.engine.on_eof)
        self.mpv.set_volume(self.volume, self.muted)
        self.mpv.clear_osd()

        if not self.settings.cms_url:
            log.error("cms_url is not set in config.yaml.  Please set cms_url and restart.")
            self._show_message("no-cms", "cms_url is not configured",
                               "Set cms_url in config.yaml and restart the player.")
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
        for t in list(self._routines.values()) + [self._enroll_task, self._wall_task, self._fetch_task]:
            if t and not t.done():
                t.cancel()
        self.preview.stop()
        try:
            await self.engine.release()
        except Exception:
            pass
        if self._ndi_receiver:
            self._ndi_receiver.stop()
        if self._ndi_sender:
            self._ndi_sender.stop()
        if ndi_engine:
            ndi_engine.cleanup()
        if self.local_api:
            self.local_api.stop()
        self.telemetry.save()
        if hasattr(self.mpv, "terminate"):
            self.mpv.terminate()
        await self.cms.aclose()

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
        if status == "registered":
            self._set_online(True, fetch=False)
            self.spawn_fetch()
        else:
            self._set_online(False)

    async def _probe(self) -> str:
        body = {"status": "online", "timestamp": datetime.now(timezone.utc).isoformat(),
                "capture_devices": [], "decklink_devices": [], "decklink_supported": False,
                "ndi_sources": [], "ndi_supported": False}
        try:
            r = await asyncio.wait_for(self.cms.heartbeat(self.player_id, body, timeout=PROBE_TIMEOUT),
                                       PROBE_TIMEOUT + 1)
        except Exception as e:
            log.info(f"[STARTUP] CMS unreachable ({type(e).__name__}) — playing offline")
            return "offline"
        if r.status_code == 404:
            return "unregistered"
        return "registered" if 200 <= r.status_code < 300 else "offline"

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
        }
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
        self.mpv.clear_osd()
        self._show_waiting()
        self.start_routines()
        self._set_online(True, fetch=False)
        self.spawn_fetch()

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
        self._wall_key = self._ndi_active_key = None
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
        if self._wall_task and not self._wall_task.done():
            st.update(status="playing", playback_status="playing", position=None, duration=None)
        body = {
            "status": st["status"],
            "playback_status": st["playback_status"],
            "position": st["position"],
            "duration": st["duration"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "current_content_id": st["current_content_id"],
            "current_content_name": st["current_content_name"],
            "playlist_index": st["playlist_index"],
            "playlist_total": st["playlist_total"],
            "scene_id": None,
            "home_scene_id": None,
            "local_ip": self.local_ip,
            "api_port": LOCAL_API_PORT,
            "player_version": VERSION,
            "uptime_seconds": int(time.time() - self._started_wall),
            "error": self.compute_error(),
            "capture_devices": [],
            "decklink_devices": [],
            "decklink_supported": False,
            "decklink_reason": "DeckLink capture is not supported by the Linux player",
            "ndi_available": _ndi_available(),
            "ndi_sources": [{"name": s.get("name")} for s in self.ndi_sources if s.get("name")],
            "ndi_supported": _ndi_available(),
            "ndi_reason": None if _ndi_available() else "NDI runtime (ndi-python) not installed",
            "av_hold_ms": None,
            "av_offset_ms": None,
        }
        body.update(metrics())
        if self._ndi_sender and self._ndi_sender.active:
            body["ndi_broadcast_active"] = True
            body["ndi_broadcast_name"] = self._ndi_sender.source_name
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
        cur = self.engine.current
        err = getattr(self.browser, "last_error", None)
        self.faults.put(F.RENDERER, F.renderer_message(err)
                        if err and cur is not None and cur.kind not in (VIDEO, "image") else None)
        return self.faults.top()

    def _set_online(self, online: bool, fetch: bool = True) -> None:
        prev, self.online = self.online, online
        if online and prev is not True:
            if prev is False:
                log.info("CMS reachable again")
                if fetch:
                    self.spawn_fetch()
            self._cancel_offline_notice()
            self.mpv.clear_osd()
            if not self.engine.has_content and not (self._wall_task and not self._wall_task.done()):
                self._show_waiting()
        elif not online and prev is not False:
            log.warning("CMS unreachable — offline")
            if self.engine.has_content:
                self.mpv.show_osd("Server offline\nPlaying your saved content — it will update "
                                  "when the server is back online.", int(OFFLINE_NOTICE_SECONDS * 1000))
                self._cancel_offline_notice()
                self._offline_notice = asyncio.get_running_loop().call_later(
                    OFFLINE_NOTICE_SECONDS, self.mpv.clear_osd)
            else:
                self._show_message("offline", "Server offline",
                                   "Trying to reconnect… content will start once the server is back online.")

    def _cancel_offline_notice(self) -> None:
        if self._offline_notice:
            self._offline_notice.cancel()
            self._offline_notice = None

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
            if e.mode == "stopped" and e.items:
                await e.play()
                return True, "replaying", None
            ok, msg = await e.replay()
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
        return False, not_supported(cmd), None

    def _apply_volume(self, volume: int, muted: bool) -> None:
        self.volume, self.muted = volume, muted
        self.mpv.set_volume(volume, muted)
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
        if data.get("desired_brightness") is not None and not self._brightness_logged:
            log.info(f"desired_brightness={data['desired_brightness']} (no backlight control on this player)")
            self._brightness_logged = True

    # -- state commands ------------------------------------------------------

    async def _state_command(self, cmd: str, a: dict) -> None:
        if cmd == "load_playlist":
            incoming = a.get("playlist_hash") or a.get("playlist_id")
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
                if not self.mpv.core_idle:
                    return
                log.info("[WALL STREAM] same stream but mpv idle — reopening")
            self._cancel_wall()
            self._ndi_active_key = key
            self._wall_task = asyncio.ensure_future(self._play_wall_rtp(url, crop))

        elif cmd == "stop_wall":
            log.info("[WALL] stop_wall — back to the playlist")
            self._leave_walls()
            await self.engine.resume()

        elif cmd == "show_ndi":
            name = a.get("source_name") or a.get("ndi_source_name")
            if name and self._ndi_receiver:
                key = f"ndi_{name}"
                if key == self._ndi_active_key:
                    return
                self._ndi_active_key = key
                await self.engine.suspend()
                self.mpv.stop()
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
                self.mpv.stop()
                n = normalise_crop(crop)
                ndi_crop = {"x": n[0], "y": n[1], "w": n[2], "h": n[3]} if n else None
                await asyncio.to_thread(self._ndi_receiver.start, name, ndi_crop)

        elif cmd == "start_ndi_broadcast":
            if self._ndi_sender:
                self._ndi_sender.start(a.get("ndi_broadcast_name") or f"{self.player_id}-Output")

        elif cmd == "stop_ndi_broadcast":
            if self._ndi_sender:
                self._ndi_sender.stop()

    @staticmethod
    def _duration(a: dict) -> float:
        try:
            return max(0.0, float(a.get("duration") or 0))
        except (TypeError, ValueError):
            return 0.0

    def _cancel_wall(self) -> None:
        if self._wall_task and not self._wall_task.done():
            self._wall_task.cancel()
        self._wall_task = None

    def _leave_walls(self) -> None:
        """A playlist or direct pick takes the screen back from any wall."""
        self._cancel_wall()
        self._wall_key = None
        self._ndi_active_key = None
        if self._ndi_receiver and self._ndi_receiver.active:
            self._ndi_receiver.stop()
        self.engine.set_crop(None)

    async def _play_wall_rtp(self, url: str, crop: Optional[dict]) -> None:
        """Server-streamed wall: open the multicast stream and crop this
        display's region. If mpv goes idle (the server restarted ffmpeg), the
        key is cleared so the next heartbeat re-opens it."""
        vf = crop_filter_pixel(crop)
        log.info(f"[WALL STREAM] Opening {url} vf={vf!r}")
        await self.engine.suspend()
        self.mpv.stop()
        try:
            await asyncio.sleep(0.3)
            for k, v in STREAM_TUNING.items():
                self.mpv.set(k, v)
            self.mpv.play_file(stream_url_with_buffers(url), vf=vf)
            await asyncio.sleep(5)
            while True:
                await asyncio.sleep(2)
                if self.mpv.core_idle:
                    log.warning("[WALL STREAM] Stream died — clearing key for auto-recovery")
                    self._ndi_active_key = None
                    return
        except asyncio.CancelledError:
            log.info("[WALL STREAM] stopped (switching content)")
        finally:
            self.mpv.set("keep-open", "always")
            self.mpv.set("cache", "auto")
            self.mpv.set("profile", "default")

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

    async def load_single_content(self, content_id: str, duration: float = 0.0, force: bool = False) -> bool:
        """A content pushed directly (not in a playlist): fetch its full
        metadata so a template/URL/RSS is classified like a playlist item."""
        meta = None
        for attempt in range(4):
            try:
                meta = await self.cms.content_meta(content_id)
                break
            except RateLimited as e:
                await asyncio.sleep(min(5.0, max(1.0, e.retry_after or 1.0)))
            except Exception as e:
                log.warning(f"[CONTENT] metadata for {content_id} unavailable: {e}")
                break
        if not meta:
            meta = self._offline_record(content_id)
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

    async def _template_canvas(self, item: Item, kept: set, network: bool) -> None:
        """The template's design canvas, for scaling its render to the screen."""
        if not item.template_id:
            return
        name = f"template_{item.template_id}.json"
        kept.add(name)
        path = self.paths.cache_dir / name
        definition = None
        if network and self.player_id:
            definition = await self.cms.template_definition(self.player_id, item.template_id)
            if definition:
                try:
                    write_json_atomic(path, definition)
                except OSError:
                    pass
        if definition is None:
            try:
                definition = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
        try:
            item.canvas = (int(definition.get("width") or 1920), int(definition.get("height") or 1080))
        except (TypeError, ValueError, AttributeError):
            pass

    async def process_playlist(self, data: dict, force: bool = False, prune: bool = True,
                               network: bool = True) -> bool:
        """Save the offline copy, classify, download what is missing (playing
        as soon as the first item is ready), then retain/prune the cache."""
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
                await self._template_canvas(item, kept, network)
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

        self.pending.keep_only({it.content_id for it in final if it.is_media})
        key = "|".join(f"{it.path or it.url}:{it.duration:g}" for it in final)

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
            log.info(f"Need to download {len(to_download)} file(s); playback starts once the first item is ready.")
            started = False
            if not force and self.engine.ready(final[0]):
                await self.engine.load(final)
                started = True
            for i, item in enumerate(to_download, 1):
                log.info(f"[DOWNLOADING {i}/{len(to_download)}] {item.name}")
                ok = await self._download(item)
                if ok and not started and self.engine.ready(final[0]):
                    await self.engine.load(final)
                    started = True
                elif ok:
                    await self.engine.kick()
            await self.engine.load(final)
            self._last_sent_key = key

        if prune:
            self.cache.retain_and_prune(kept)
        else:
            self.cache.enforce_lru(kept)
        return True

    async def _download(self, item: Item) -> bool:
        lock = self._dl_locks.setdefault(str(item.path), asyncio.Lock())
        async with lock:
            try:
                path = await self.cms.download(item.content_id, item.path)
            except ContentNotReady as e:
                log.info(f"[DOWNLOAD] {item.name} not ready yet (retry in {e.retry_after or 30:.0f}s)")
                self.pending.not_ready(item.content_id, item.path, item.name, e.retry_after)
                return False
            except ContentGone as e:
                log.error(f"[DOWNLOAD] {item.name} no longer exists ({e}) — not retrying")
                self._gone.add(item.content_id)
                self.pending.done(item.content_id)
                return False
            except (CmsError, httpx.HTTPError, OSError) as e:
                log.error(f"[DOWNLOAD] {item.name} failed: {e}")
                self.pending.failed(item.content_id, item.path, item.name)
                await asyncio.sleep(2)
                return False
            item.path = repair_name(path)
            self.pending.done(item.content_id)
            self._gone.discard(item.content_id)
            log.info(f"[SUCCESS] Downloaded {item.path.name}")
            return True

    async def _pending_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(PENDING_INTERVAL)
            if not self.player_id or self.online is False:
                continue
            for p in self.pending.due():
                item = next((it for it in self.engine.items if it.content_id == p.content_id), None)
                if item is None:
                    self.pending.done(p.content_id)
                    continue
                if await self._download(item):
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
    # SSE, telemetry, info
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
            if self.online is False:
                continue
            try:
                await self._flush_telemetry()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug(f"[TELEMETRY] flush failed: {e}")

    async def _info_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.local_ip = await asyncio.to_thread(lan_address_for, self.settings.cms_url)
                if _ndi_available():
                    self.ndi_sources = await asyncio.to_thread(ndi_engine.find_sources, timeout_ms=2000)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug(f"[INFO] refresh failed: {e}")
            await asyncio.sleep(INFO_INTERVAL)

    # ------------------------------------------------------------------
    # Preview / local API helpers (run on worker threads)
    # ------------------------------------------------------------------

    def _grab_image(self):
        try:
            from PIL import Image, ImageGrab
        except ImportError:
            return None
        if self.browser.running:
            try:
                return ImageGrab.grab(xdisplay=self.settings.display)
            except Exception as e:
                log.debug(f"[PREVIEW] screen grab failed: {e}")
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

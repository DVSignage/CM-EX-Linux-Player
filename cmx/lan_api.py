"""
The player's control API on port 8083 — a port of DVSI Plus `Cmx/CmxLocalApi.cs`
and `Cmx/LocalApiPolicy.cs`.

Loopback by default. With LAN triggers switched on (`lan_api.enabled`) it
listens on every interface, so a sensor or show controller on the venue
network can drive an interactive wall directly — no cloud, no edge.

Interactive-wall routes (a sensor or controller sends no Origin header):
  POST /api/trigger       {"command":"trigger","id":3}  (id also as a numeric
                          string, as "trigger_id", or ?id=3 with an empty body)
  POST /api/scene         {"scene":"…"} (or "scene_id"; root / parent / back;
                          "stop": true ends what plays first)
  POST /api/home          end what plays, back to home
  POST /api/back          end what plays, back to the previous scene
  POST /api/play-content  {"content_id":"…"} one of the wall's own clips, fullscreen
  POST /api/alignment     {"enabled": true}
  GET  /api/interaction   what the wall is doing
  POST /api/patch         {"element_id":"…","text":"…"} or {"element_id":"…","content_id":"…"}
Every answer is {accepted, reason, scene}; accepted:false with a reason is a
normal outcome, not an HTTP error.

Everything else (play / pause / restart, device, preview, capture and NDI
lists) is for this machine only and needs `X-Local-Key` (local-api.key), and
answers with no CORS header: a web page shown on the screen must not be able
to drive the player. A browser calling a wall route is judged as a LAN caller
(it needs X-Trigger-Key when one is set), and /api/patch is refused to a
browser outright unless a key is set.
"""

import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("player")

PORT = 8083
MAX_BODY = 4096
LOCAL_KEY_HEADER = "X-Local-Key"
TRIGGER_KEY_HEADER = "X-Trigger-Key"
WALL_ROUTES = {"/api/trigger", "/api/scene", "/api/alignment", "/api/interaction", "/api/patch",
               "/api/home", "/api/back", "/api/play-content"}
PREVIEW = {"fps": 3, "width": 640, "height": 360, "quality": 50}

ALLOW, UNAUTHORIZED, FORBIDDEN, NOT_FOUND = "allow", "unauthorized", "forbidden", "not_found"


@dataclass(frozen=True)
class Decision:
    verdict: str
    cors: bool


def decide(path: str, method: str, is_local: bool, has_origin: bool,
           wall_key_configured: bool, wall_key_ok: bool, local_key_ok: bool) -> Decision:
    """Who may call what — DVSI Plus LocalApiPolicy.Decide, case for case."""
    if path in WALL_ROUTES:
        if method == "OPTIONS":
            return Decision(ALLOW, True)
        if path == "/api/patch" and has_origin and not wall_key_configured:
            return Decision(FORBIDDEN, True)
        as_lan = not is_local or has_origin
        if as_lan and not wall_key_ok:
            return Decision(UNAUTHORIZED, True)
        return Decision(ALLOW, True)
    if path == "/api/sys-reboot":
        return Decision(NOT_FOUND, False)
    if not is_local:
        return Decision(FORBIDDEN, False)
    if method == "OPTIONS":
        return Decision(NOT_FOUND, False)
    if not local_key_ok:
        return Decision(UNAUTHORIZED, False)
    return Decision(ALLOW, False)


def keys_match(sent: Optional[str], key: Optional[str]) -> bool:
    if not key:
        return False
    return hmac.compare_digest((sent or "").encode(), key.encode())


def wall_command(path: str, body: dict, query: dict):
    """The wall route → (command, args) for the player, or (None, refusal)."""
    cmd = {"/api/scene": "set_scene", "/api/play-content": "wall_play"}.get(path, path[len("/api/"):])
    if cmd in ("home", "back"):
        return "set_scene", {"scene": "root" if cmd == "home" else "back", "stop": True}
    if cmd == "trigger":
        named = str(body.get("command") or body.get("cmd") or "").strip()
        if named and named.lower() != "trigger":
            return None, f"'{named}' is not a trigger"
        if body.get("id") is None and body.get("trigger_id") is None and query.get("id"):
            body = dict(body, id=query["id"][0])
    return cmd, body


class LanApiServer:
    """`handle_wall(cmd, args) -> (accepted, reason, scene)` and the other
    callbacks run on the player's asyncio loop (the server thread waits)."""

    def __init__(self, local_key: str, *, lan_enabled: bool, trigger_key: str,
                 handle_wall: Callable, snapshot: Callable[[], dict], control: Callable[[str], None],
                 device_info: Callable[[], dict], capture_devices: Callable[[], list],
                 ndi_sources: Callable[[], dict], preview_jpeg: Callable[[], Optional[bytes]],
                 reset_registration: Optional[Callable[[], str]] = None, port: int = PORT):
        self.port = port
        self.lan_enabled = lan_enabled
        self.trigger_key = trigger_key or ""
        self._local_key = local_key
        self._handle_wall = handle_wall
        self._snapshot = snapshot
        self._control = control
        self._device_info = device_info
        self._capture_devices = capture_devices
        self._ndi_sources = ndi_sources
        self._preview_jpeg = preview_jpeg
        self._reset_registration = reset_registration
        self._server: Optional[ThreadingHTTPServer] = None
        self.lan_bound = False

    def start(self) -> Optional[str]:
        """Listen (on every interface when LAN triggers are on); returns why
        it could not, or None."""
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _send(self, status: int, body: bytes = b"", ctype: str = "application/json",
                      cors: bool = False, extra: Optional[dict] = None):
                self.send_response(status)
                if body or ctype:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if cors:
                    self.send_header("Access-Control-Allow-Origin", "*")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body and self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, data, status: int = 200, cors: bool = False):
                self._send(status, json.dumps(data).encode(), cors=cors)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_OPTIONS(self):
                self._route("OPTIONS")

            def _route(self, method: str):
                url = urlsplit(self.path)
                path = url.path.rstrip("/").lower()
                wall = path in WALL_ROUTES
                d = decide(path, method,
                           is_local=self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1"),
                           has_origin=bool(self.headers.get("Origin")),
                           wall_key_configured=bool(api.trigger_key),
                           wall_key_ok=not api.trigger_key or keys_match(self.headers.get(TRIGGER_KEY_HEADER), api.trigger_key),
                           local_key_ok=not wall and keys_match(self.headers.get(LOCAL_KEY_HEADER), api._local_key))
                if d.verdict == UNAUTHORIZED:
                    if wall:
                        return self._json({"accepted": False, "reason": "bad key"}, 401, d.cors)
                    return self._json({"error": f"this route needs the {LOCAL_KEY_HEADER} header (local-api.key)"}, 401, d.cors)
                if d.verdict == FORBIDDEN:
                    if wall:
                        return self._json({"accepted": False,
                                           "reason": "a browser may not change the template without the trigger key"}, 403, d.cors)
                    return self._send(403, cors=d.cors)
                if d.verdict == NOT_FOUND:
                    return self._send(404, cors=d.cors)
                try:
                    if wall:
                        return self._wall(method, path, parse_qs(url.query))
                    return self._local(method, path)
                except Exception as e:   # the player must never fall over on a request
                    log.warning(f"[LAN API] {path} failed: {e}")
                    return self._json({"accepted": False, "reason": "internal error"} if wall else {"error": "internal error"},
                                      500, cors=wall)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return None
                raw = self.rfile.read(n) if n else b""
                return raw.decode("utf-8", "replace")

            def _wall(self, method: str, path: str, query: dict):
                if method == "OPTIONS":
                    return self._send(204, ctype="", cors=True, extra={
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                        "Access-Control-Allow-Headers": f"Content-Type, {TRIGGER_KEY_HEADER}"})
                if path == "/api/interaction":
                    if method != "GET":
                        return self._send(405, cors=True)
                    snap = dict(api._snapshot())
                    snap.update(lan_triggers=api.lan_bound, api_port=api.port)
                    return self._json(snap, cors=True)
                if method != "POST":
                    return self._send(405, cors=True)
                raw = self._body()
                if raw is None:
                    return self._json({"accepted": False, "reason": "body too large"}, 413, True)
                try:
                    body = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    body = None
                if not isinstance(body, dict):
                    return self._json({"accepted": False, "reason": "body is not a JSON object"}, 400, True)
                cmd, args = wall_command(path, body, query) if path != "/api/patch" else ("patch", body)
                if cmd is None:
                    return self._json({"accepted": False, "reason": args}, 400, True)
                ok, reason, scene = api._handle_wall(cmd, args)
                return self._json({"accepted": bool(ok), "reason": reason, "scene": scene}, cors=True)

            def _local(self, method: str, path: str):
                if method == "POST" and path in ("/api/play", "/api/pause", "/api/restart"):
                    api._control(path[len("/api/"):])
                    return self._json({"status": "success"})
                if method == "POST" and path == "/api/reset-registration" and api._reset_registration:
                    return self._json({"status": "success", "message": api._reset_registration()})
                if method == "GET" and path == "/api/device":
                    return self._json(api._device_info())
                if method == "GET" and path == "/api/preview/config":
                    return self._json(PREVIEW)
                if method == "GET" and path == "/api/preview/snapshot":
                    jpeg = api._preview_jpeg()
                    if not jpeg:
                        return self._send(503, ctype="")
                    return self._send(200, jpeg, "image/jpeg")
                if method == "GET" and path == "/api/preview/stream":
                    return self._stream()
                if method == "GET" and path == "/api/capture/devices":
                    return self._json({"devices": api._capture_devices(), "decklink_devices": [],
                                       "decklink_supported": False,
                                       "decklink_reason": "DeckLink capture is not supported by the Linux player"})
                if method == "GET" and path == "/api/ndi/sources":
                    return self._json(api._ndi_sources())
                return self._send(404, ctype="")

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    while True:
                        jpeg = api._preview_jpeg()
                        if jpeg:
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                            self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode() + jpeg + b"\r\n")
                            self.wfile.flush()
                        time.sleep(1 / PREVIEW["fps"])
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                log.debug("[LAN API] a connection dropped")

        hosts = ["0.0.0.0"] if self.lan_enabled else []
        hosts.append("127.0.0.1")
        last = None
        for host in hosts:
            try:
                self._server = Server((host, self.port), Handler)
                self.port = self._server.server_address[1]
                self.lan_bound = host == "0.0.0.0"
                break
            except OSError as e:
                last = str(e)
                self._server = None
        if self._server is None:
            log.error(f"[LAN API] cannot listen on port {self.port}: {last}")
            return last
        threading.Thread(target=self._server.serve_forever, daemon=True, name="lan-api").start()
        if self.lan_bound:
            log.info(f"[LAN API] port {self.port} on all interfaces — interactive-wall triggers accepted from the LAN"
                     + (" (X-Trigger-Key required)" if self.trigger_key else " (no trigger key set)"))
        else:
            log.info(f"[LAN API] port {self.port} on this machine only")
        return None

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

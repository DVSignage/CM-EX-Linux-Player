"""
Local HTTP API on port 8081 (reported to the CMS as `api_port`).

Open to the LAN, as the CMS calls them: `/api/health`, `/api/device`,
`/api/ndi/sources`.

Hardened as in DVSI Plus 1.13.0: the screen preview routes
(`/api/preview/snapshot`, `/api/preview/stream`) — now superseded by the
pushed preview — answer only loopback callers or a caller presenting
`X-Local-Key`, a random key kept owner-only in `local-api.key`.
"""

import base64
import hmac
import json
import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("player")

PORT = 8081
PROTECTED = ("/api/preview/snapshot", "/api/preview/stream")


def load_or_create_key(path: Path) -> str:
    try:
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    except OSError:
        pass
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key)
    except OSError as e:
        log.warning(f"[LOCAL API] could not save key: {e}")
    return key


def allowed(path: str, client_ip: str, presented_key: Optional[str], key: str) -> bool:
    """The access decision: open routes for everyone, protected routes for
    loopback or the right key."""
    route = path.split("?", 1)[0]
    if route not in PROTECTED:
        return True
    if client_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return True
    return bool(presented_key) and hmac.compare_digest(presented_key, key)


class LocalApiServer:
    def __init__(self, key_path: Path, device_info: Callable[[], dict],
                 ndi_sources: Callable[[], list], snapshot: Callable[[], Optional[bytes]],
                 port: int = PORT):
        self.port = port
        self._key = load_or_create_key(key_path)
        self._device_info = device_info
        self._ndi_sources = ndi_sources
        self._snapshot = snapshot
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> Optional[str]:
        """Start serving; returns why it could not, or None."""
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _json(self, data, status=200):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                route = self.path.split("?", 1)[0]
                if not allowed(route, self.client_address[0], self.headers.get("X-Local-Key"), api._key):
                    self._json({"error": "forbidden"}, 403)
                    return
                try:
                    if route == "/api/health":
                        self._json({"status": "ok"})
                    elif route == "/api/device":
                        self._json(api._device_info())
                    elif route == "/api/ndi/sources":
                        self._json(api._ndi_sources())
                    elif route == "/api/preview/snapshot":
                        jpeg = api._snapshot()
                        if not jpeg:
                            self._json({"error": "Unable to capture frame"}, 503)
                            return
                        self.send_response(200)
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Content-Length", str(len(jpeg)))
                        self.end_headers()
                        self.wfile.write(jpeg)
                    elif route == "/api/preview/stream":
                        self.send_response(200)
                        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        try:
                            while True:
                                jpeg = api._snapshot()
                                if jpeg:
                                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                                    self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                                    self.wfile.write(jpeg + b"\r\n")
                                    self.wfile.flush()
                                time.sleep(0.333)
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                    else:
                        self._json({"error": "Not found"}, 404)
                except Exception as e:
                    log.debug(f"[LOCAL API] {route} failed: {e}")
                    try:
                        self._json({"error": str(e)}, 500)
                    except Exception:
                        pass

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Local-Key")
                self.end_headers()

        try:
            self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
            self._server.daemon_threads = True
        except OSError as e:
            log.error(f"[LOCAL API] cannot listen on port {self.port}: {e}")
            return str(e)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="local-api")
        self._thread.start()
        log.info(f"Local API server listening on port {self.port}")
        return None

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

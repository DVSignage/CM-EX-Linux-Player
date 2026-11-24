"""
The renderer server: the local web page Chromium shows full screen.

Loopback only (127.0.0.1). It serves
  /                  the renderer page (index.html, /static/*)
  /media/<name>      files from the content cache, with Range (video seeking)
  /screens/<name>    generated pictures (the enrollment QR code)
  /events            a Server-Sent Events stream: what the page should show
  /api/event   POST  what the page reports back (ended, status, taps, ...)
  /api/rss/<cid>     the cached headlines of a feed

The player side talks to it through `send()` (to every page) and an event
callback that is handed to the asyncio loop thread-safely. The last "show"
message is replayed to a page that (re)connects, so a crashed and restarted
browser comes back showing the same thing.
"""

import json
import logging
import mimetypes
import queue
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import unquote

log = logging.getLogger("player")

PORT = 8083
STATIC_DIR = Path(__file__).parent / "static"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._\-]+$")
# Message types that describe what is on screen; the latest of each group is
# replayed to a page that connects.
STICKY = {"screen", "media", "web", "template", "capture", "stop"}
STICKY_EXTRA = {"volume", "config", "overlay", "crop", "pause"}

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("text/javascript", ".js")


class RendererServer:
    def __init__(self, cache_dir: Path, screens_dir: Path, rss_dir: Path,
                 on_event: Callable[[dict], None], port: int = PORT, host: str = "127.0.0.1"):
        self.cache_dir = cache_dir
        self.screens_dir = screens_dir
        self.rss_dir = rss_dir
        self.port = port
        self.host = host
        self._on_event = on_event
        self._clients: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._last_show: Optional[dict] = None
        self._last_extra: Dict[str, dict] = {}
        self._server: Optional[ThreadingHTTPServer] = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._clients)

    # -- player → page -----------------------------------------------------

    def send(self, msg: dict) -> None:
        t = msg.get("type")
        with self._lock:
            if t in STICKY:
                self._last_show = msg
                if t != "media":
                    self._last_extra.pop("crop", None)
            elif t in STICKY_EXTRA:
                self._last_extra[t] = msg
            clients = list(self._clients)
        data = json.dumps(msg)
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    def _replay(self) -> List[str]:
        with self._lock:
            out = [json.dumps(m) for k, m in self._last_extra.items() if k != "pause"]
            if self._last_show is not None:
                out.append(json.dumps(self._last_show))
            if "pause" in self._last_extra and self._last_show and self._last_show.get("type") == "media":
                out.append(json.dumps(self._last_extra["pause"]))
        return out

    # -- serving -------------------------------------------------------------

    def start(self) -> Optional[str]:
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, data, status=200):
                self._send(status, json.dumps(data).encode(), "application/json")

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                path = unquote(self.path.split("?", 1)[0])
                if path in ("/", "/index.html"):
                    return self._file(STATIC_DIR / "index.html")
                if path.startswith("/static/"):
                    return self._named(STATIC_DIR, path[len("/static/"):])
                if path.startswith("/media/"):
                    return self._named(srv.cache_dir, path[len("/media/"):], ranged=True)
                if path.startswith("/screens/"):
                    return self._named(srv.screens_dir, path[len("/screens/"):])
                if path.startswith("/api/rss/"):
                    name = path[len("/api/rss/"):]
                    if not _SAFE_NAME.match(name):
                        return self._json({"items": []}, 404)
                    try:
                        return self._send(200, (srv.rss_dir / f"{name}.json").read_bytes(), "application/json")
                    except OSError:
                        return self._json({"items": []})
                if path == "/events":
                    return self._events()
                return self._json({"error": "not found"}, 404)

            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if path != "/api/event":
                    return self._json({"error": "not found"}, 404)
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    msg = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._json({"error": "bad json"}, 400)
                if isinstance(msg, dict):
                    try:
                        srv._on_event(msg)
                    except Exception as e:
                        log.debug(f"[RENDERER] event handler failed: {e}")
                self._json({"ok": True})

            def _named(self, base: Path, name: str, ranged: bool = False):
                if not _SAFE_NAME.match(name):
                    return self._json({"error": "bad name"}, 400)
                return self._file(base / name, ranged)

            def _file(self, path: Path, ranged: bool = False):
                try:
                    size = path.stat().st_size
                except OSError:
                    return self._json({"error": "not found"}, 404)
                ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                start, end = 0, size - 1
                status = 200
                rng = self.headers.get("Range") if ranged else None
                m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
                if m and size > 0:
                    if m.group(1):
                        start = int(m.group(1))
                        if m.group(2):
                            end = min(int(m.group(2)), size - 1)
                    elif m.group(2):
                        start = max(0, size - int(m.group(2)))
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
                length = end - start + 1 if size else 0
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-cache")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if self.command == "HEAD" or not length:
                    return
                try:
                    with open(path, "rb") as f:
                        f.seek(start)
                        left = length
                        while left > 0:
                            chunk = f.read(min(262144, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _events(self):
                q: queue.Queue = queue.Queue(maxsize=500)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                for data in srv._replay():
                    q.put_nowait(data)
                with srv._lock:
                    srv._clients.append(q)
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            data = q.get(timeout=15)
                            self.wfile.write(f"data: {data}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with srv._lock:
                        if q in srv._clients:
                            srv._clients.remove(q)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                log.debug("[RENDERER] a page connection dropped")   # a closing browser, not an error

        try:
            self._server = Server((self.host, self.port), Handler)
            self.port = self._server.server_address[1]
        except OSError as e:
            log.error(f"[RENDERER] cannot listen on {self.host}:{self.port}: {e}")
            return str(e)
        threading.Thread(target=self._server.serve_forever, daemon=True, name="renderer").start()
        log.info(f"Renderer page at {self.url}")
        return None

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

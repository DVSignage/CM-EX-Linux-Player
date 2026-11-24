"""
An in-memory CM:EX for tests and local end-to-end runs.

  - `FakeCms.handler` is an httpx.MockTransport handler (unit tests);
  - `python tests/fake_cms.py --port 8000 --media DIR` serves the same
    contract over HTTP so a real player (mpv + Chromium) can be driven
    end to end. Approve the code with `POST /_admin/approve`, queue a
    command with `POST /_admin/command` (JSON body), read acks at
    `GET /_admin/acks` and heartbeats at `GET /_admin/beats`.
"""

import argparse
import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import httpx


class FakeCms:
    def __init__(self, player_id: str = "P1", secret: str = "s3cret"):
        self.player_id = player_id
        self.secret = secret
        self.code = "ABC-123"
        self.approved = False
        self.registered = True
        self.playlist: Optional[dict] = None
        self.media: dict = {}            # content_id -> bytes
        self.meta: dict = {}             # content_id -> content object
        self.templates: dict = {}        # template id -> definition
        self.rss: dict = {}              # feed content id -> items
        self.commands = deque()          # heartbeat responses to hand out
        self.acks = []
        self.beats = []
        self.telemetry = []
        self.preview_frames = []
        self.secrets_seen = []
        self.lock = threading.Lock()

    # -- shared routing ---------------------------------------------------

    def route(self, method: str, path: str, headers: dict, body: bytes):
        """→ (status, content-type, bytes, extra headers)."""
        p = path.split("?", 1)[0]
        pid = self.player_id
        if p.startswith(f"/api/v1/players/{pid}/"):
            self.secrets_seen.append(headers.get("x-player-secret"))
        if method == "POST" and p == "/api/v1/players/enroll/request":
            return self._json({"code": self.code, "site_id": "site-1", "site_name": "Lobby"})
        if method == "GET" and p == f"/api/v1/players/enroll/{self.code}/status":
            if not self.approved:
                return 404, "application/json", b"{}", {}
            return self._json({"status": "approved", "player_id": pid, "player_secret": self.secret})
        if method == "POST" and p == f"/api/v1/players/{pid}/heartbeat":
            if not self.registered:
                return 404, "application/json", b"{}", {}
            with self.lock:
                self.beats.append(json.loads(body or b"{}"))
                reply = self.commands.popleft() if self.commands else {"command": "none"}
            return self._json(reply)
        if method == "GET" and p == f"/api/v1/players/{pid}/assigned-playlist":
            return 200, "application/json", json.dumps(self.playlist).encode(), {}
        if method == "POST" and p == f"/api/v1/players/{pid}/ack":
            with self.lock:
                self.acks.append(json.loads(body))
            return self._json({"accepted": True})
        if method == "POST" and p == f"/api/v1/players/{pid}/telemetry":
            events = json.loads(body).get("events", [])
            with self.lock:
                self.telemetry.extend(events)
            return self._json({"accepted": len(events), "duplicates": 0, "rejected": 0})
        if method == "POST" and p == f"/api/v1/players/{pid}/preview-frame":
            with self.lock:
                self.preview_frames.append(body)
            return self._json({"watching": True})
        if p.startswith("/api/v1/content/") and p.endswith("/stream"):
            cid = p.split("/")[4]
            if cid not in self.media:
                return 404, "text/plain", b"gone", {}
            return 200, "application/octet-stream", self.media[cid], {}
        if method == "GET" and p.startswith("/api/v1/content/"):
            cid = p.split("/")[4]
            if cid in self.meta:
                return self._json(self.meta[cid])
            return 404, "application/json", b"{}", {}
        if p.startswith(f"/api/v1/players/{pid}/templates/"):
            tid = p.rsplit("/", 1)[1]
            if self.templates:
                return self._json(self.templates[tid]) if tid in self.templates else (404, "application/json", b"{}", {})
            return self._json({"width": 3840, "height": 2160, "elements": []})
        if p.startswith("/api/v1/rss/") and p.endswith("/items"):
            return self._json({"items": self.rss.get(p.split("/")[4], [])})
        return 404, "application/json", b'{"error":"not found"}', {}

    @staticmethod
    def _json(data):
        return 200, "application/json", json.dumps(data).encode(), {}

    # -- httpx.MockTransport ------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        status, ctype, data, extra = self.route(request.method, request.url.raw_path.decode(),
                                                headers, request.content)
        return httpx.Response(status, content=data, headers={"Content-Type": ctype, **extra})


def serve(cms: FakeCms, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _send(self, status, ctype, data):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method):
            body = self._body()
            if self.path == "/_admin/approve":
                cms.approved = True
                return self._send(200, "application/json", b'{"ok":true}')
            if self.path == "/_admin/command":
                cms.commands.append(json.loads(body))
                return self._send(200, "application/json", b'{"ok":true}')
            if self.path == "/_admin/unregister":
                cms.registered = False
                return self._send(200, "application/json", b'{"ok":true}')
            if self.path == "/_admin/acks":
                return self._send(200, "application/json", json.dumps(cms.acks).encode())
            if self.path == "/_admin/beats":
                return self._send(200, "application/json", json.dumps(cms.beats[-3:]).encode())
            if self.path == "/_admin/telemetry":
                return self._send(200, "application/json", json.dumps(cms.telemetry).encode())
            if self.path.endswith("/events"):
                return self._send(204, "text/plain", b"")
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, ctype, data, _ = cms.route(method, self.path, headers, body)
            self._send(status, ctype, data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    ap = argparse.ArgumentParser(description="Fake CM:EX for end-to-end player runs")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--media", type=Path, required=True,
                    help="folder of media files; each file becomes a content item")
    args = ap.parse_args()
    cms = FakeCms()
    items = []
    for i, f in enumerate(sorted(p for p in args.media.iterdir() if p.is_file())):
        cid = f"c{i}"
        cms.media[cid] = f.read_bytes()
        cms.meta[cid] = {"type": "video", "filename": f.name}
        items.append({"content_id": cid, "duration": 8, "content": {"filename": f.name, "type": "video"}})
    cms.playlist = {"id": "pl1", "items": items}
    serve(cms, args.port)
    print(f"Fake CM:EX on :{args.port} with {len(items)} item(s). Ctrl+C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

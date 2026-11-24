"""
HTTP client for the CM:EX REST contract (`/api/v1`).

Mirrors DVSI Plus `Cmx/CmsClient.cs`:
  - 5 s connect / 30 s request timeouts;
  - the httpx client is rebuilt every 60 s and after a transport error, so a
    connection or DNS answer cached while the network was still coming up at
    boot cannot keep the player offline for ever;
  - `X-Player-Secret` on every player-scoped request when the screen has one;
  - downloads resume a leftover `.tmp` with `Range`, and a 503 + Retry-After
    means "not ready yet", not a failure;
  - edge mode: requests are routed by class through EdgeRouter (cmx/edge.py).
"""

import asyncio
import email.utils
import json
import logging
import time
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

import httpx

from .edge import CLOUD_ONLY, CONTROL, MEDIA, TELEMETRY, EdgeRouter

log = logging.getLogger("player")

CONNECT_TIMEOUT = 5.0
REQUEST_TIMEOUT = 30.0
CLIENT_LIFETIME = 60.0
DOWNLOAD_CHUNK = 65536
TELEMETRY_PATHS = ("telemetry", "events")


class CmsError(Exception):
    """A request the CMS refused or could not answer."""


class RateLimited(CmsError):
    def __init__(self, retry_after: Optional[float]):
        super().__init__(f"rate limited (retry after {retry_after})")
        self.retry_after = retry_after


class ContentNotReady(CmsError):
    """503 on a download: the asset exists but is not ready yet."""

    def __init__(self, retry_after: Optional[float]):
        super().__init__(f"content not ready (retry after {retry_after})")
        self.retry_after = retry_after


class ContentGone(CmsError):
    """404/410 on a download: the asset no longer exists; never retried."""


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Retry-After as seconds: delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


class CmsClient:
    def __init__(self, base_url: str, secret: Callable[[], Optional[str]],
                 transport: Optional[httpx.AsyncBaseTransport] = None,
                 router: Optional[EdgeRouter] = None):
        self.base_url = base_url.rstrip("/")
        self._secret = secret
        self._transport = transport   # tests inject httpx.MockTransport
        self.router = router or EdgeRouter("cloud", "", self.base_url)
        self._client: Optional[httpx.AsyncClient] = None
        self._client_born = 0.0
        self._rebuild = False
        self._telemetry_path: Optional[str] = None
        self.last_from_edge = False

    # ------------------------------------------------------------------
    # URLs (pages a renderer loads are always built from the cloud base)
    # ------------------------------------------------------------------

    def api(self, path: str, base: Optional[str] = None) -> str:
        return f"{(base or self.base_url).rstrip('/')}/api/v1{path}"

    def stream_url(self, content_id: str) -> str:
        return self.api(f"/content/{content_id}/stream")

    def template_render_url(self, template_id: str) -> str:
        return self.api(f"/templates/{template_id}/render")

    def rss_render_url(self, content_id: str) -> str:
        return self.api(f"/rss/{content_id}/render")

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        now = time.monotonic()
        stale = self._client is None or self._client.is_closed or self._rebuild \
            or now - self._client_born > CLIENT_LIFETIME
        if stale:
            old = self._client
            if old is not None and not old.is_closed:
                asyncio.ensure_future(old.aclose())
            kwargs = dict(timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT))
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.AsyncClient(**kwargs)
            self._client_born = now
            self._rebuild = False
        return self._client

    def _headers(self, player_scoped: bool, extra: Optional[dict] = None) -> dict:
        h = dict(extra or {})
        if player_scoped:
            s = self._secret()
            if s:
                h["X-Player-Secret"] = s
        return h

    async def _send(self, method: str, url: str, player_scoped: bool, timeout, headers, **kwargs) -> httpx.Response:
        try:
            return await self._http().request(
                method, url, headers=self._headers(player_scoped, headers),
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT, **kwargs)
        except httpx.TransportError:
            self._rebuild = True
            raise

    async def request(self, method: str, path: str, *, cls: str = CONTROL, player_scoped: bool = True,
                      timeout: Optional[float] = None, headers: Optional[dict] = None,
                      base: Optional[str] = None, **kwargs) -> httpx.Response:
        """One request, routed by class in edge mode: an edge failure that
        trips the class over is retried against the cloud straight away."""
        if path.startswith("http"):
            self.last_from_edge = False
            return await self._send(method, path, player_scoped, timeout, headers, **kwargs)
        routed = base is None and cls in (CONTROL, MEDIA) and self.router.is_edge(cls)
        target = base or self.router.base_for(cls)
        self.last_from_edge = routed or (base is None and cls == TELEMETRY and self.router.enabled)
        try:
            r = await self._send(method, self.api(path, target), player_scoped, timeout, headers, **kwargs)
        except (httpx.TransportError, httpx.TimeoutException):
            if routed and self.router.record(cls, None):
                self.last_from_edge = False
                return await self._send(method, self.api(path), player_scoped, timeout, headers, **kwargs)
            raise
        if routed and self.router.record(cls, r.status_code, parse_retry_after(r.headers.get("Retry-After"))):
            self.last_from_edge = False
            return await self._send(method, self.api(path), player_scoped, timeout, headers, **kwargs)
        return r

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    @staticmethod
    def _json(r: httpx.Response):
        if not r.content or not r.content.strip():
            return None
        return r.json()

    async def _edge_then_cloud(self, path: str) -> httpx.Response:
        """CLOUD_ONLY reads: the edge once first (outside the failure count),
        the cloud when the edge has no answer."""
        if self.router.enabled:
            try:
                r = await self._send("GET", self.api(path, self.router.edge_url), True, 10, None)
                if r.status_code == 200:
                    return r
            except (httpx.TransportError, httpx.TimeoutException):
                pass
        return await self.request("GET", path, cls=CLOUD_ONLY)

    # ------------------------------------------------------------------
    # Enrollment
    # ------------------------------------------------------------------

    async def enroll_request(self) -> dict:
        r = await self.request("POST", "/players/enroll/request", player_scoped=False)
        r.raise_for_status()
        return self._json(r) or {}

    async def enroll_status(self, code: str):
        """(status_code, body or {}, retry_after seconds)."""
        r = await self.request("GET", f"/players/enroll/{code}/status", player_scoped=False)
        body = {}
        if r.status_code == 200:
            body = self._json(r) or {}
        return r.status_code, body, parse_retry_after(r.headers.get("Retry-After"))

    # ------------------------------------------------------------------
    # Player routines
    # ------------------------------------------------------------------

    async def heartbeat(self, player_id: str, body: dict, timeout: Optional[float] = None) -> httpx.Response:
        return await self.request("POST", f"/players/{player_id}/heartbeat", json=body, timeout=timeout)

    async def heartbeat_cloud(self, player_id: str, body: dict, timeout: Optional[float] = 5) -> httpx.Response:
        """A heartbeat straight to the cloud, bypassing the router: is this
        player really deleted, or is the edge's replica just behind?"""
        return await self.request("POST", f"/players/{player_id}/heartbeat", json=body,
                                  timeout=timeout, base=self.base_url)

    async def assigned_playlist(self, player_id: str) -> Optional[dict]:
        """The assigned playlist, or None when nothing is assigned (a bare
        `null` or an empty body is "nothing", not an error)."""
        r = await self.request("GET", f"/players/{player_id}/assigned-playlist")
        r.raise_for_status()
        data = self._json(r)
        return data if isinstance(data, dict) else None

    async def ack(self, player_id: str, command_id, command: str, success: bool, message: str) -> bool:
        """POST the result of an enveloped command. One retry after 500 ms on
        a transport error or 5xx; a 4xx ("I never issued that id") is final."""
        body = {"success": bool(success), "message": message, "command_id": command_id, "command": command}
        for attempt in (1, 2):
            try:
                r = await self.request("POST", f"/players/{player_id}/ack", json=body)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                log.warning(f"[ACK] {command} ({command_id}) transport error: {e}")
                if attempt == 1:
                    await asyncio.sleep(0.5)
                    continue
                return False
            if 200 <= r.status_code < 300:
                try:
                    data = self._json(r) or {}
                except ValueError:
                    data = {}
                accepted = data.get("accepted", True) if isinstance(data, dict) else True
                if not accepted:
                    log.warning(f"[ACK] {command} ({command_id}) not accepted by the CMS")
                return bool(accepted)
            log.warning(f"[ACK] {command} ({command_id}) answered HTTP {r.status_code}")
            if r.status_code >= 500 and attempt == 1:
                await asyncio.sleep(0.5)
                continue
            return False
        return False

    async def content_meta(self, content_id: str) -> dict:
        r = await self._edge_then_cloud(f"/content/{content_id}")
        if r.status_code == 429:
            raise RateLimited(parse_retry_after(r.headers.get("Retry-After")))
        r.raise_for_status()
        data = self._json(r)
        return data if isinstance(data, dict) else {}

    async def template_definition(self, player_id: str, template_id: str) -> Optional[dict]:
        """The player-scoped read first (no CMS login needed), then the plain one."""
        for path in (f"/players/{player_id}/templates/{template_id}", f"/templates/{template_id}"):
            try:
                r = await self._edge_then_cloud(path)
            except (httpx.TransportError, httpx.TimeoutException):
                return None
            if r.status_code == 200:
                data = self._json(r)
                if isinstance(data, dict):
                    return data
        return None

    async def rss_items(self, content_id: str) -> Optional[dict]:
        r = await self._edge_then_cloud(f"/rss/{content_id}/items")
        if r.status_code != 200:
            return None
        data = self._json(r)
        return data if isinstance(data, dict) else None

    async def telemetry(self, player_id: str, events: list) -> httpx.Response:
        """POST telemetry (`{"events": [...]}` — a bare array reads as nothing).
        Edge mode: `/telemetry`, falling back to `/events` on a 404, the path
        that answers remembered (forgotten again on 404/405). Cloud mode:
        `/telemetry` only — `/events` there is the SSE stream."""
        body = {"events": events}
        if not self.router.enabled:
            return await self.request("POST", f"/players/{player_id}/telemetry", cls=TELEMETRY, json=body, timeout=10)
        paths = [self._telemetry_path] if self._telemetry_path else list(TELEMETRY_PATHS)
        r = None
        for p in paths:
            r = await self.request("POST", f"/players/{player_id}/{p}", cls=TELEMETRY, json=body, timeout=10)
            if r.status_code in (404, 405):
                self._telemetry_path = None
                continue
            if 200 <= r.status_code < 300:
                self._telemetry_path = p
            return r
        return r

    async def preview_frame(self, player_id: str, jpeg: bytes) -> Optional[bool]:
        """POST one preview JPEG, straight to the cloud. Returns whether
        someone is still watching, or None when the CMS refused the frame."""
        try:
            r = await self.request("POST", f"/players/{player_id}/preview-frame", base=self.base_url,
                                   content=jpeg, headers={"Content-Type": "image/jpeg"}, timeout=8)
        except (httpx.TransportError, httpx.TimeoutException):
            return None
        if not 200 <= r.status_code < 300:
            return None
        try:
            data = self._json(r) or {}
        except ValueError:
            data = {}
        return bool(data.get("watching", True)) if isinstance(data, dict) else True

    async def site_info(self) -> Optional[dict]:
        """The edge's own idea of the venue it serves (edge mode)."""
        if not self.router.enabled:
            return None
        try:
            r = await self._send("GET", self.api("/site", self.router.edge_url), False, 5, None)
            return self._json(r) if r.status_code == 200 else None
        except (httpx.TransportError, httpx.TimeoutException, ValueError):
            return None

    async def probe_edge(self) -> bool:
        """`/api/v1/healthz` then `/healthz`: 2xx = up; 404 on both = up too."""
        base = self.router.edge_url
        statuses = []
        for url in (f"{base}/api/v1/healthz", f"{base}/healthz"):
            try:
                r = await self._send("GET", url, False, 5, None)
            except (httpx.TransportError, httpx.TimeoutException):
                return False
            if 200 <= r.status_code < 300:
                return True
            statuses.append(r.status_code)
        return all(s == 404 for s in statuses)

    async def events(self, player_id: str) -> AsyncIterator[dict]:
        """The SSE push channel: one JSON object per `data:` line. The request
        timeout covers the response headers only; reads wait indefinitely."""
        base = self.router.base_for(CONTROL)
        url = self.api(f"/players/{player_id}/events", base)
        timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT, read=None)
        headers = self._headers(True, {"Accept": "text/event-stream"})
        try:
            async with self._http().stream("GET", url, headers=headers, timeout=timeout) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        continue
                    if isinstance(data, dict):
                        yield data
        except httpx.TransportError:
            self._rebuild = True
            if self.router.is_edge(CONTROL):
                self.router.record(CONTROL, None)
            raise

    # ------------------------------------------------------------------
    # Downloads
    # ------------------------------------------------------------------

    async def download(self, content_id: str, dest: Path,
                       progress: Optional[Callable[[int, Optional[int]], None]] = None) -> Path:
        """Download a content item to `dest`, resuming `dest.tmp` with Range.

        206 appends; 200 restarts the .tmp; 416 deletes it (the next try
        starts clean). Raises ContentNotReady (503), ContentGone (404/410),
        RateLimited (429) or CmsError. Edge mode: MEDIA class, failing over."""
        edge = self.router.is_edge(MEDIA)
        try:
            return await self._download_from(self.router.base_for(MEDIA), content_id, dest, progress, edge)
        except (httpx.TransportError, httpx.TimeoutException):
            if edge and self.router.record(MEDIA, None):
                return await self._download_from(self.base_url, content_id, dest, progress, False)
            raise
        except _EdgeTripped:
            return await self._download_from(self.base_url, content_id, dest, progress, False)

    async def _download_from(self, base: str, content_id: str, dest: Path, progress, edge: bool) -> Path:
        tmp = Path(str(dest) + ".tmp")
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have > 0 else {}
        url = self.api(f"/content/{content_id}/stream", base)
        try:
            async with self._http().stream("GET", url, headers=self._headers(True, headers)) as r:
                retry_after = parse_retry_after(r.headers.get("Retry-After"))
                if edge and self.router.record(MEDIA, r.status_code, retry_after):
                    raise _EdgeTripped()
                if r.status_code == 503:
                    raise ContentNotReady(retry_after)
                if r.status_code in (404, 410):
                    raise ContentGone(f"HTTP {r.status_code}")
                if r.status_code == 429:
                    raise RateLimited(retry_after)
                if r.status_code == 416:
                    tmp.unlink(missing_ok=True)
                    raise CmsError("range not satisfiable — partial file discarded")
                if r.status_code not in (200, 206):
                    raise CmsError(f"HTTP {r.status_code}")
                append = r.status_code == 206
                total = None
                if append:
                    cr = r.headers.get("Content-Range", "")
                    if "/" in cr and cr.rsplit("/", 1)[1].strip().isdigit():
                        total = int(cr.rsplit("/", 1)[1])
                elif r.headers.get("Content-Length", "").isdigit():
                    total = int(r.headers["Content-Length"])
                done = have if append else 0
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "ab" if append else "wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=DOWNLOAD_CHUNK):
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
        except httpx.TransportError:
            self._rebuild = True
            raise
        tmp.replace(dest)
        return dest


class _EdgeTripped(Exception):
    """The edge's answer tripped MEDIA over to the cloud: retry there."""

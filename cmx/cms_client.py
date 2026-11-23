"""
HTTP client for the CM:EX REST contract (`/api/v1`).

Mirrors DVSI Plus `Cmx/CmsClient.cs`, cloud mode:
  - 5 s connect / 30 s request timeouts;
  - the httpx client is rebuilt every 60 s and after a transport error, so a
    connection or DNS answer cached while the network was still coming up at
    boot cannot keep the player offline for ever;
  - `X-Player-Secret` on every player-scoped request when the screen has one;
  - downloads resume a leftover `.tmp` with `Range`, and a 503 + Retry-After
    means "not ready yet", not a failure.
"""

import asyncio
import email.utils
import json
import logging
import time
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

import httpx

log = logging.getLogger("player")

CONNECT_TIMEOUT = 5.0
REQUEST_TIMEOUT = 30.0
CLIENT_LIFETIME = 60.0
DOWNLOAD_CHUNK = 65536


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
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.base_url = base_url.rstrip("/")
        self._secret = secret
        self._transport = transport   # tests inject httpx.MockTransport
        self._client: Optional[httpx.AsyncClient] = None
        self._client_born = 0.0
        self._rebuild = False

    # ------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------

    def api(self, path: str) -> str:
        return f"{self.base_url}/api/v1{path}"

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

    async def request(self, method: str, path: str, *, player_scoped: bool = True,
                      timeout: Optional[float] = None, headers: Optional[dict] = None,
                      **kwargs) -> httpx.Response:
        url = path if path.startswith("http") else self.api(path)
        try:
            return await self._http().request(
                method, url, headers=self._headers(player_scoped, headers),
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                **kwargs)
        except httpx.TransportError:
            self._rebuild = True
            raise

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    @staticmethod
    def _json(r: httpx.Response):
        if not r.content or not r.content.strip():
            return None
        return r.json()

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

    async def heartbeat(self, player_id: str, body: dict,
                        timeout: Optional[float] = None) -> httpx.Response:
        return await self.request("POST", f"/players/{player_id}/heartbeat",
                                  json=body, timeout=timeout)

    async def assigned_playlist(self, player_id: str) -> Optional[dict]:
        """The assigned playlist, or None when nothing is assigned (a bare
        `null` or an empty body is "nothing", not an error)."""
        r = await self.request("GET", f"/players/{player_id}/assigned-playlist")
        r.raise_for_status()
        data = self._json(r)
        return data if isinstance(data, dict) else None

    async def ack(self, player_id: str, command_id, command: str,
                  success: bool, message: str) -> bool:
        """POST the result of an enveloped command. One retry after 500 ms on
        a transport error or 5xx; a 4xx ("I never issued that id") is final."""
        body = {"success": bool(success), "message": message,
                "command_id": command_id, "command": command}
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
        r = await self.request("GET", f"/content/{content_id}")
        if r.status_code == 429:
            raise RateLimited(parse_retry_after(r.headers.get("Retry-After")))
        r.raise_for_status()
        data = self._json(r)
        return data if isinstance(data, dict) else {}

    async def template_definition(self, player_id: str, template_id: str) -> Optional[dict]:
        """The player-scoped read first (no CMS login needed), then the plain one."""
        for path in (f"/players/{player_id}/templates/{template_id}",
                     f"/templates/{template_id}"):
            try:
                r = await self.request("GET", path)
            except (httpx.TransportError, httpx.TimeoutException):
                return None
            if r.status_code == 200:
                data = self._json(r)
                if isinstance(data, dict):
                    return data
        return None

    async def telemetry(self, player_id: str, events: list) -> httpx.Response:
        # The body is an object: a bare array reads as "no events" upstream.
        return await self.request("POST", f"/players/{player_id}/telemetry",
                                  json={"events": events}, timeout=10)

    async def preview_frame(self, player_id: str, jpeg: bytes) -> Optional[bool]:
        """POST one preview JPEG. Returns whether someone is still watching,
        or None when the CMS refused the frame."""
        try:
            r = await self.request("POST", f"/players/{player_id}/preview-frame",
                                   content=jpeg, headers={"Content-Type": "image/jpeg"},
                                   timeout=8)
        except (httpx.TransportError, httpx.TimeoutException):
            return None
        if not 200 <= r.status_code < 300:
            return None
        try:
            data = self._json(r) or {}
        except ValueError:
            data = {}
        return bool(data.get("watching", True)) if isinstance(data, dict) else True

    async def events(self, player_id: str) -> AsyncIterator[dict]:
        """The SSE push channel: one JSON object per `data:` line. The request
        timeout covers the response headers only; reads wait indefinitely."""
        url = self.api(f"/players/{player_id}/events")
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
            raise

    # ------------------------------------------------------------------
    # Downloads
    # ------------------------------------------------------------------

    async def download(self, content_id: str, dest: Path,
                       progress: Optional[Callable[[int, Optional[int]], None]] = None) -> Path:
        """Download a content item to `dest`, resuming `dest.tmp` with Range.

        206 appends; 200 restarts the .tmp; 416 deletes it (the next try
        starts clean). Raises ContentNotReady (503), ContentGone (404/410),
        RateLimited (429) or CmsError."""
        tmp = Path(str(dest) + ".tmp")
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have > 0 else {}
        url = self.stream_url(content_id)
        try:
            async with self._http().stream("GET", url, headers=self._headers(True, headers)) as r:
                if r.status_code == 503:
                    raise ContentNotReady(parse_retry_after(r.headers.get("Retry-After")))
                if r.status_code in (404, 410):
                    raise ContentGone(f"HTTP {r.status_code}")
                if r.status_code == 429:
                    raise RateLimited(parse_retry_after(r.headers.get("Retry-After")))
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

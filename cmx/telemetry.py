"""
Telemetry: a spooled event queue and the stability events DVSI Plus sends.

Queue rules (DVSI Plus `Cmx/CmxTelemetry.cs`):
  - each event gets an `event_id` once, when queued; retries re-send it
    verbatim (the CMS's dedup key) and the same id is never queued twice;
  - spooled to telemetry-queue.json (written atomically), so an outage
    survives a restart; cap 5000, oldest non-proof-of-play evicted first;
  - body `{"events": [...]}`, at most 500 per POST;
  - 2xx with counts removes what was taken; 400/422 halves the batch until
    the bad event is alone, then drops only it; other 4xx, 5xx and transport
    errors keep everything queued.

In cloud mode only `edge_event` (and `inventory`) go to the cloud's
`POST /players/{id}/telemetry`; `proof_of_play`/`player_health` are edge-only.
"""

import json
import logging
import os
import re
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

import httpx

log = logging.getLogger("player")

QUEUE_CAP = 5000
MAX_BATCH = 500
CLOUD_TYPES = {"edge_event", "inventory"}
PRODUCT = "CM:EX Linux Player"
DETAIL_CAP = 1800
MESSAGE_CAP = 1000
MAX_CRASH_REPORTS = 10


def ts(dt: Optional[datetime] = None) -> str:
    """ISO-8601 UTC with milliseconds and a Z."""
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Scrubbing — text that leaves the machine carries no personal data
# ---------------------------------------------------------------------------

_URL = re.compile(r"\b(?:https?|wss?|ftp)://[^\s\"'<>]+", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SECRET = re.compile(
    r"\b(password|passwd|pwd|secret|token|api[_\-]?key|authorization)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
    re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/\-]+=*", re.IGNORECASE)
_CMX_TOKEN = re.compile(r"\bcmx_[A-Za-z0-9_\-]{8,}")
_HOME = re.compile(r"(/home/)[^/\s\"'<>]+")


def _strip_url(m: re.Match) -> str:
    url = m.group(0)
    scheme, _, rest = url.partition("://")
    rest = rest.split("#", 1)[0].split("?", 1)[0]
    if "@" in rest.split("/", 1)[0]:
        rest = rest.split("@", 1)[1]
    return f"{scheme}://{rest}"


def scrub(text: str) -> str:
    """URLs lose query, fragment and userinfo; e-mail addresses, credential
    values, bearer and CMS tokens are replaced; the account in a home path is
    replaced. Types, frames, hosts and codes are kept: they diagnose a crash."""
    if not text:
        return text
    text = _URL.sub(_strip_url, text)
    text = _EMAIL.sub("<email>", text)
    # Bearer first: "Authorization: Bearer <token>" would otherwise lose only
    # the word "Bearer" to the key=value rule and leak the token.
    text = _BEARER.sub("Bearer <redacted>", text)
    text = _SECRET.sub(lambda m: m.group(0) if m.group(3) == "Bearer" else
                       m.group(1) + m.group(2) + "<redacted>", text)
    text = _CMX_TOKEN.sub("cmx_<redacted>", text)
    text = _HOME.sub(r"\1<user>", text)
    return text


def cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def edge_event(player_id: str, level: str, code: str, message: str, detail,
               when: Optional[datetime] = None, event_id: Optional[str] = None) -> dict:
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "type": "edge_event",
        "player_uuid": player_id,
        "ts": ts(when),
        "level": level,
        "code": code,
        "message": cap(message, MESSAGE_CAP),
        "detail": detail,
    }


def boot_event(player_id: str, version: str, started: datetime, after_crash: bool) -> dict:
    """Once per process start; ts is when the process started."""
    return edge_event(player_id, "info", "boot", f"{PRODUCT} {version} started",
                      {"version": version, "after_crash": after_crash}, when=started)


def crash_event(player_id: str, marker: dict) -> dict:
    short = (marker.get("exception_type") or "Unknown").rsplit(".", 1)[-1]
    text = marker.get("stack") or f"{marker.get('exception_type')}: {marker.get('message')}"
    try:
        when = datetime.fromisoformat(str(marker.get("ts", "")).replace("Z", "+00:00"))
    except ValueError:
        when = None
    return edge_event(player_id, "error", "crash", f"{PRODUCT} crashed: {short}",
                      cap(scrub(text), DETAIL_CAP), when=when, event_id=marker.get("event_id"))


# ---------------------------------------------------------------------------
# Crash markers — written when the process dies, reported at the next start
# ---------------------------------------------------------------------------

def write_crash_marker(crash_dir: Path, version: str, exc: BaseException) -> Optional[Path]:
    try:
        crash_dir.mkdir(parents=True, exist_ok=True)
        event_id = str(uuid.uuid4())
        marker = {
            "event_id": event_id,
            "ts": ts(),
            "version": version,
            "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "message": str(exc),
            "stack": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[:8000],
        }
        path = crash_dir / f"crash-{event_id}.json"
        path.write_text(json.dumps(marker), encoding="utf-8")
        return path
    except Exception:
        return None


def install_crash_hook(crash_dir: Path, version: str) -> None:
    """Record an uncaught exception before the process dies."""
    previous = sys.excepthook

    def hook(etype, value, tb):
        if not issubclass(etype, (KeyboardInterrupt, SystemExit)):
            write_crash_marker(crash_dir, version, value)
        previous(etype, value, tb)

    sys.excepthook = hook


def pending_crash_markers(crash_dir: Path) -> List[Path]:
    """The newest few crash files; older ones are deleted unreported."""
    try:
        files = sorted(crash_dir.glob("crash-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for old in files[MAX_CRASH_REPORTS:]:
        try:
            old.unlink()
        except OSError:
            pass
    return files[:MAX_CRASH_REPORTS]


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

Sender = Callable[[List[dict]], Awaitable[httpx.Response]]


class TelemetryQueue:
    def __init__(self, path: Path, cap_: int = QUEUE_CAP, routable=CLOUD_TYPES):
        self.path = path
        self.cap = cap_
        self.routable = set(routable)
        self.events: List[dict] = []
        self._batch = MAX_BATCH
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.events = [e for e in data if isinstance(e, dict) and e.get("event_id")]
        except (OSError, ValueError):
            self.events = []
        self._enforce_cap()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.events), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning(f"[TELEMETRY] could not spool queue: {e}")

    def _enforce_cap(self) -> None:
        while len(self.events) > self.cap:
            idx = next((i for i, e in enumerate(self.events) if e.get("type") != "proof_of_play"), 0)
            del self.events[idx]

    def enqueue(self, event: dict) -> bool:
        """Queue an event (keeping its id). False when that id is already queued."""
        event.setdefault("event_id", str(uuid.uuid4()))
        if any(e.get("event_id") == event["event_id"] for e in self.events):
            return False
        self.events.append(event)
        self._enforce_cap()
        self.save()
        return True

    def _sendable(self) -> List[dict]:
        return [e for e in self.events if e.get("type") in self.routable]

    def _remove(self, batch: List[dict]) -> None:
        ids = {e["event_id"] for e in batch}
        self.events = [e for e in self.events if e.get("event_id") not in ids]

    async def flush(self, send: Sender) -> int:
        """Send what is routable until done or the server stops taking it.
        Returns how many events left the queue."""
        sent = 0
        while True:
            pending = self._sendable()
            if not pending:
                break
            batch = pending[: self._batch]
            try:
                r = await send(batch)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                log.info(f"[TELEMETRY] not delivered ({type(e).__name__}); kept {len(pending)} queued")
                break
            if 200 <= r.status_code < 300:
                try:
                    body = r.json() if r.content else {}
                except ValueError:
                    body = {}
                body = body if isinstance(body, dict) else {}
                if not any(k in body for k in ("accepted", "duplicates", "rejected")):
                    self._remove(batch)
                    sent += len(batch)
                    self._batch = MAX_BATCH
                    continue
                taken = int(body.get("accepted") or 0) + int(body.get("duplicates") or 0)
                if taken >= len(batch):
                    self._remove(batch)
                    sent += len(batch)
                    self._batch = MAX_BATCH
                    continue
                if len(batch) == 1:
                    if int(body.get("rejected") or 0) > 0:
                        log.error(f"[TELEMETRY] event rejected and dropped: {batch[0].get('code') or batch[0].get('type')}")
                        self._remove(batch)
                        continue
                    break   # taken by nobody, refused by nobody: try again later
                self._batch = max(1, len(batch) // 2)
                continue
            if r.status_code in (400, 422):
                if len(batch) == 1:
                    log.error(f"[TELEMETRY] schema refused {batch[0].get('code') or batch[0].get('type')}; dropped it")
                    self._remove(batch)
                    self._batch = MAX_BATCH
                else:
                    self._batch = max(1, len(batch) // 2)
                continue
            log.info(f"[TELEMETRY] HTTP {r.status_code}; kept {len(pending)} queued")
            break
        self.save()
        return sent

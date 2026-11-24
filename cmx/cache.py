"""
The content cache: file naming, picture/video sniffing, retention and the
pending-download list.

Ports of DVSI Plus behaviour:
  - `Cmx/MediaSniff.cs` — a file's first bytes decide picture vs video, and
    a cache file whose name says the wrong one is renamed;
  - RetainRecentPlaylists — keep the files of the last N distinct playlists
    (cache-history.json) and prune to their union;
  - EnforceCacheCap — direct picks never prune, they cap unassigned files;
  - the pending-download backoff (30 s → 1 m → 2 m → 5 m → 10 m → 30 m).
"""

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .items import is_image_name

log = logging.getLogger("player")

CACHE_LIMIT_BYTES = 50 * 1024 ** 3
MAX_UNASSIGNED = 1000
HEAD_LENGTH = 16

UNKNOWN, IMAGE, VIDEO = "unknown", "image", "video"


# ---------------------------------------------------------------------------
# Media sniffing
# ---------------------------------------------------------------------------

def identify(head: bytes) -> Tuple[str, Optional[str]]:
    """(kind, extension) from a file's first bytes."""
    h = head
    if len(h) >= 8 and h[:8] == b"\x89PNG\r\n\x1a\n":
        return IMAGE, ".png"
    if len(h) >= 3 and h[:3] == b"\xff\xd8\xff":
        return IMAGE, ".jpg"
    if len(h) >= 6 and h[:4] == b"GIF8" and h[4:5] in (b"7", b"9") and h[5:6] == b"a":
        return IMAGE, ".gif"
    if len(h) >= 12 and h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return IMAGE, ".webp"
    if len(h) >= 14 and h[:2] == b"BM":
        return IMAGE, ".bmp"
    if len(h) >= 12 and h[4:8] == b"ftyp":
        return VIDEO, ".mov" if h[8:12] == b"qt  " else ".mp4"
    if len(h) >= 4 and h[:4] == b"\x1a\x45\xdf\xa3":
        return VIDEO, ".webm"
    if len(h) >= 12 and h[:4] == b"RIFF" and h[8:12] == b"AVI ":
        return VIDEO, ".avi"
    if len(h) >= 8 and h[:8] == b"\x30\x26\xb2\x75\x8e\x66\xcf\x11":
        return VIDEO, ".wmv"
    return UNKNOWN, None


def identify_file(path: Path) -> Tuple[str, Optional[str]]:
    try:
        with open(path, "rb") as f:
            return identify(f.read(HEAD_LENGTH))
    except OSError:
        return UNKNOWN, None


def is_image_file(path: Path) -> bool:
    """The bytes decide; the name only when they say nothing."""
    kind, _ = identify_file(path)
    if kind == IMAGE:
        return True
    if kind == VIDEO:
        return False
    return is_image_name(path.name)


def misnamed_as(path: Path) -> Optional[str]:
    """The extension a file should have when its bytes and name disagree on
    picture vs video; None when they agree or the bytes say nothing."""
    kind, ext = identify_file(path)
    if ext is None:
        return None
    named_image = is_image_name(path.name)
    if (kind == IMAGE and not named_image) or (kind == VIDEO and named_image):
        return ext
    return None


def repair_name(path: Path) -> Path:
    """Rename a file whose bytes contradict its name; returns the path to use.
    A copy already under the right name wins and the misnamed one is removed."""
    right = misnamed_as(path)
    if right is None:
        return path
    to = path.with_suffix(right)
    try:
        if to.exists():
            path.unlink()
            log.info(f"[CACHE] {path.name} duplicated {to.name} under the wrong name; removed it")
        else:
            path.rename(to)
            log.info(f"[CACHE] {path.name} is a {'picture' if right in ('.png', '.jpg', '.gif', '.webp', '.bmp') else 'video'} by its bytes; renamed it {to.name}")
        return to
    except OSError as e:
        log.warning(f"[CACHE] {path.name} has the wrong name for its bytes and could not be renamed ({e})")
        return path


# ---------------------------------------------------------------------------
# Cache store
# ---------------------------------------------------------------------------

class CacheStore:
    def __init__(self, cache_dir: Path, history_path: Path, keep_playlists: int = 10,
                 limit_bytes: int = CACHE_LIMIT_BYTES):
        self.dir = cache_dir
        self.history_path = history_path
        self.keep_playlists = keep_playlists
        self.limit_bytes = limit_bytes

    def size_bytes(self) -> int:
        total = 0
        try:
            for f in self.dir.iterdir():
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    def is_full(self) -> bool:
        return self.size_bytes() >= self.limit_bytes

    def find(self, content_id: str, preferred: Path) -> Optional[Path]:
        """The cached file for a content id: the preferred name if present,
        else any `{content_id}.*` (repairing a misnamed one on the way)."""
        if preferred.exists():
            return preferred
        try:
            candidates = sorted(p for p in self.dir.glob(f"{content_id}.*")
                                if p.is_file() and not p.name.endswith(".tmp")
                                and ".snapshot." not in p.name and not p.name.endswith(".json"))
        except OSError:
            return None
        for p in candidates:
            fixed = repair_name(p)
            if fixed.exists():
                return fixed
        return None

    # -- retention ----------------------------------------------------------

    def _load_history(self) -> List[List[str]]:
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            return [list(map(str, s)) for s in data if isinstance(s, list)]
        except (OSError, ValueError):
            return []

    def retain(self, names: Iterable[str]) -> Set[str]:
        """Record this playlist's file set (most recent first, deduped by
        content, trimmed to N) and return the union of the kept sets."""
        current = sorted(set(names))
        history = [s for s in self._load_history() if sorted(set(s)) != current]
        history.insert(0, current)
        history = history[: self.keep_playlists]
        try:
            self.history_path.write_text(json.dumps(history), encoding="utf-8")
        except OSError as e:
            log.warning(f"[CACHE] could not save history: {e}")
        union: Set[str] = set()
        for s in history:
            union.update(s)
        return union

    def prune_to(self, keep: Set[str]) -> int:
        """Delete every cache file not in `keep` (never a .tmp in progress)."""
        removed = 0
        lower = {n.lower() for n in keep}
        try:
            files = list(self.dir.iterdir())
        except OSError:
            return 0
        for f in files:
            if not f.is_file() or f.name.endswith(".tmp") or f.name.lower() in lower:
                continue
            try:
                f.unlink()
                removed += 1
                log.info(f"[GC] Deleted {f.name}")
            except OSError as e:
                log.error(f"[GC] Delete error {f.name}: {e}")
        return removed

    def retain_and_prune(self, names: Iterable[str]) -> int:
        return self.prune_to(self.retain(names))

    def enforce_lru(self, keep: Iterable[str], max_unassigned: int = MAX_UNASSIGNED) -> int:
        """Direct picks don't prune: cap files outside `keep` and the retained
        playlists to the newest `max_unassigned`."""
        protected = {n.lower() for n in keep}
        for s in self._load_history():
            protected.update(n.lower() for n in s)
        try:
            others = [f for f in self.dir.iterdir()
                      if f.is_file() and not f.name.endswith(".tmp") and f.name.lower() not in protected]
        except OSError:
            return 0
        others.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        removed = 0
        for f in others[max_unassigned:]:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def clear(self) -> None:
        """Delete the whole cache and its history (re-enrollment)."""
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            self.history_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Pending downloads
# ---------------------------------------------------------------------------

BACKOFF = [30, 60, 120, 300, 600, 1800]


@dataclass
class Pending:
    content_id: str
    path: Path
    name: str
    retry_at: float
    attempts: int = 0


class PendingDownloads:
    """Assets the playlist references but could not download yet. The
    playlist still applies; each entry is retried on its own backoff for as
    long as the playlist keeps it."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.entries: Dict[str, Pending] = {}

    def not_ready(self, cid: str, path: Path, name: str, retry_after: Optional[float]) -> None:
        """503 + Retry-After: the edge has not verified the bytes yet."""
        self.entries[cid] = Pending(cid, path, name, self._clock() + (retry_after or BACKOFF[0]), 0)

    def failed(self, cid: str, path: Path, name: str) -> None:
        cur = self.entries.get(cid)
        attempts = (cur.attempts if cur else 0) + 1
        wait = BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
        self.entries[cid] = Pending(cid, path, name, self._clock() + wait, attempts)

    def done(self, cid: str) -> None:
        self.entries.pop(cid, None)

    def keep_only(self, cids: Set[str]) -> None:
        for cid in list(self.entries):
            if cid not in cids:
                del self.entries[cid]

    def due(self) -> List[Pending]:
        now = self._clock()
        return [p for p in self.entries.values() if p.retry_at <= now]

    def fault(self) -> Optional[str]:
        """The heartbeat `error` wording once a download has failed ≥3 times."""
        bad = [p for p in self.entries.values() if p.attempts >= 3]
        if not bad:
            return None
        worst = max(bad, key=lambda p: p.attempts)
        if len(bad) > 1:
            return f"{len(bad)} assets could not be downloaded (including {worst.name}); still retrying"
        return f"{worst.name} could not be downloaded after {worst.attempts} attempts; still retrying"

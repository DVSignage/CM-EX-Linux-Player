"""
Enrollment rules: how long to wait between status polls, and the Connect:EX
deep link shown as a QR code beside the registration code.

Ports of DVSI Plus `Cmx/EnrollPollPolicy.cs` and `Cmx/EnrollLink.cs`.
"""

from typing import Optional
from urllib.parse import quote, urlsplit

POLL_INTERVAL = 5.0      # normal gap between polls
BUSY_WAIT = 10.0         # 503 without Retry-After (edge: cloud unreachable)
RATE_LIMITED_WAIT = 30.0  # 429 without Retry-After (CM:EX per-address limit)
MAX_WAIT = 60.0          # never let a server hide an approval for longer
CODE_POLL_CAP = 600.0    # drop a code nobody approved after 10 minutes
NEW_CODE_RETRY = 10.0    # wait after a failed code request

_MAX_SITE = 64


def _clamp(seconds: float) -> float:
    return min(MAX_WAIT, max(POLL_INTERVAL, seconds))


def poll_wait(status: int, retry_after: Optional[float]) -> Optional[float]:
    """The wait before the next poll when `status` means "still pending, keep
    the same code" (404, 503, 429); None for any other answer."""
    if status == 404:
        return POLL_INTERVAL
    if status == 503:
        return _clamp(retry_after if retry_after is not None else BUSY_WAIT)
    if status == 429:
        return _clamp(retry_after if retry_after is not None else RATE_LIMITED_WAIT)
    return None


def _origin(cms_url: Optional[str]) -> Optional[str]:
    """scheme://host[:port][/path] without a trailing slash or /api/v1."""
    if not cms_url or not cms_url.strip():
        return None
    try:
        u = urlsplit(cms_url.strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.netloc:
        return None
    path = u.path.rstrip("/")
    if path.lower().endswith("/api/v1"):
        path = path[: -len("/api/v1")].rstrip("/")
    return f"{u.scheme}://{u.netloc}{path}"


def enroll_link(cms_url: Optional[str], code: Optional[str], site: Optional[str] = None) -> Optional[str]:
    """dvsiconnect://enroll?v=1&system=cmex&server=<origin>&code=<CODE>[&site=<id>],
    or None without an http(s) CMS address or a code."""
    server = _origin(cms_url)
    c = (code or "").strip().upper()
    if server is None or not c:
        return None
    link = ("dvsiconnect://enroll?v=1&system=cmex"
            f"&server={quote(server, safe='')}"
            f"&code={quote(c, safe='')}")
    s = (site or "").strip()
    if s:
        link += f"&site={quote(s[:_MAX_SITE], safe='')}"
    return link

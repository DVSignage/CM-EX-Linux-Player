"""
Edge (LAN) mode routing — a port of DVSI Plus `Cmx/EdgeRouter.cs`.

In edge mode the player talks to a site edge server first and the cloud CMS
is the failover. Request classes:

  CONTROL     heartbeat, assigned playlist, ack, SSE, enrollment — edge
              first, cloud after 2 consecutive failures
  MEDIA       content downloads — the same, counted separately, so one
              missing asset cannot tip the whole player over
  CLOUD_ONLY  content meta, templates, RSS — the cloud (callers ask the edge
              once first, outside the failure count)
  TELEMETRY   the edge only; never fails over

A failure is a connect error, a timeout or a 5xx; a 503 with Retry-After is
a healthy edge's backpressure and never a failure; any answer (404 too)
resets the count. While anything is failed over, a probe every 30 s fails
every class back on the first healthy answer (a forced failover — the
stale-404 hold — waits out its hold).
"""

import logging
import time
from typing import Callable, Dict, Optional

log = logging.getLogger("player")

CONTROL, MEDIA, CLOUD_ONLY, TELEMETRY = "control", "media", "cloud_only", "telemetry"
FAILOVER_AFTER = 2
PROBE_INTERVAL = 30.0


class EdgeRouter:
    def __init__(self, mode: str, edge_url: str, cloud_url: str,
                 on_transition: Optional[Callable[[str, str, str], None]] = None,
                 clock=time.monotonic):
        self.mode = mode if mode in ("cloud", "edge") else "cloud"
        self.edge_url = (edge_url or "").rstrip("/")
        self.cloud_url = (cloud_url or "").rstrip("/")
        self._on_transition = on_transition     # (code, class, reason)
        self._clock = clock
        self.failed: Dict[str, bool] = {CONTROL: False, MEDIA: False}
        self._fails: Dict[str, int] = {CONTROL: 0, MEDIA: 0}
        self._hold_until: Dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return self.mode == "edge" and bool(self.edge_url)

    @property
    def any_failed_over(self) -> bool:
        return any(self.failed.values())

    def base_for(self, cls: str) -> str:
        if not self.enabled:
            return self.cloud_url
        if cls == TELEMETRY:
            return self.edge_url
        if cls in self.failed and not self.failed[cls]:
            return self.edge_url
        return self.cloud_url

    def is_edge(self, cls: str) -> bool:
        return self.enabled and self.base_for(cls) == self.edge_url

    @staticmethod
    def is_failure(status: Optional[int], retry_after: Optional[float]) -> bool:
        """status None = transport error / timeout."""
        if status is None:
            return True
        if status == 503 and retry_after is not None:
            return False
        return status >= 500

    def record(self, cls: str, status: Optional[int], retry_after: Optional[float] = None) -> bool:
        """Count one edge answer for a class; True when this failure tripped
        the class over to the cloud (the caller retries it there)."""
        if not self.enabled or cls not in self._fails or self.failed[cls]:
            return False
        if not self.is_failure(status, retry_after):
            self._fails[cls] = 0
            return False
        self._fails[cls] += 1
        if self._fails[cls] >= FAILOVER_AFTER:
            self._fail_over(cls, f"{self._fails[cls]} consecutive failures"
                                 f" ({'no answer' if status is None else f'HTTP {status}'})")
            return True
        return False

    def _fail_over(self, cls: str, reason: str) -> None:
        self.failed[cls] = True
        self._fails[cls] = 0
        log.warning(f"[EDGE] {cls} failed over to the cloud: {reason}")
        if self._on_transition:
            self._on_transition("edge_failover", cls, reason)

    def force_failover(self, cls: str, hold_s: float, reason: str) -> None:
        self._hold_until[cls] = self._clock() + hold_s
        if not self.failed.get(cls):
            self._fail_over(cls, reason)

    def probe_result(self, healthy: bool) -> None:
        """Fail every eligible class back after a healthy probe."""
        if not healthy:
            return
        now = self._clock()
        for cls, failed in self.failed.items():
            if failed and self._hold_until.get(cls, 0) <= now:
                self.failed[cls] = False
                self._fails[cls] = 0
                log.info(f"[EDGE] {cls} failed back to the edge")
                if self._on_transition:
                    self._on_transition("edge_failback", cls, "edge healthy again")

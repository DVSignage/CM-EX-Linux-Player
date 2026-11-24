"""
CMS command envelope rules (DVSI Plus `CmxPlayerService.HandleCommand`).

  - A command with a `command_id` executes ONCE: SSE delivery does not clear
    the CMS's queue, so the next heartbeat re-delivers the same id, and a
    relative seek would otherwise double.
  - Every delivery of that id is ACKED: the CMS repeats a command until an
    ack lands, so a repeat means the last ack was lost — the recorded result
    is re-sent without re-running the command.
  - No `command_id`, no ack (the per-beat `load_playlist` re-send is state
    sync, deduplicated by playlist_hash).
  - `next`/`previous` without an id are coalesced in a 1 s window;
    `play`/`pause`/`restart` are idempotent and re-applied on every delivery.
"""

import time
from collections import OrderedDict
from typing import Optional, Tuple

# Commands executed inside the envelope; their ack carries the real outcome.
ENVELOPE_COMMANDS = {
    "play", "pause", "stop", "replay", "restart", "next", "previous", "seek",
    "refresh", "restart_service", "set_volume", "delete_player",
    "trigger", "set_scene", "wall_play", "alignment", "template_patch",
}

# State commands: re-sent on every beat, deduplicated by their own keys.
# With an id they are acked "<cmd> accepted" up front, then run.
STATE_COMMANDS = {
    "load_playlist", "load_content", "load_wall_content", "load_wall_rtp",
    "stop_wall", "show_ndi", "hide_ndi", "show_ndi_wall",
    "start_ndi_broadcast", "stop_ndi_broadcast", "show_capture", "hide_capture",
}

COALESCED = {"next", "previous"}
CONTROL_DEDUP_SECONDS = 1.0
ID_WINDOW = 64

RUN, RESEND, RUNNING = "run", "resend", "running"


def not_supported(command: str) -> str:
    return f"command '{command}' not supported by this player"


class CommandGate:
    """Execute-once / ack-every bookkeeping over the last 64 command ids."""

    def __init__(self, window: int = ID_WINDOW):
        self._window = window
        self._seen: "OrderedDict[str, Optional[Tuple[bool, str]]]" = OrderedDict()

    def claim(self, command_id) -> Tuple[str, Optional[Tuple[bool, str]]]:
        """RUN (first delivery), RESEND + recorded result, or RUNNING."""
        key = str(command_id)
        if key in self._seen:
            result = self._seen[key]
            return (RUNNING, None) if result is None else (RESEND, result)
        self._seen[key] = None
        while len(self._seen) > self._window:
            self._seen.popitem(last=False)
        return RUN, None

    def record(self, command_id, success: bool, message: str) -> None:
        key = str(command_id)
        if key in self._seen:
            self._seen[key] = (bool(success), message)


class Coalescer:
    """Drops an identical next/previous within the dedup window: the CMS
    delivers on both the heartbeat and SSE, and a double `next` skipped an item."""

    def __init__(self, window: float = CONTROL_DEDUP_SECONDS, clock=time.monotonic):
        self._window = window
        self._clock = clock
        self._last: dict = {}

    def should_run(self, command: str) -> bool:
        if command not in COALESCED:
            return True
        now = self._clock()
        last = self._last.get(command)
        self._last[command] = now
        return last is None or now - last >= self._window


def try_read_int(value) -> Optional[int]:
    """An int from a JSON number or a numeric string (rounded)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    if isinstance(value, str):
        try:
            return int(round(float(value.strip())))
        except ValueError:
            return None
    return None


def try_read_bool(value) -> Optional[bool]:
    """A bool from true/false, "true"/"false", "1"/"0", or an integer."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
    return None


def parse_volume(args: dict, current_volume: int, current_muted: bool) -> Tuple[int, bool]:
    """`set_volume {volume, muted}`; a missing or unusable value keeps the
    current setting (a type mismatch used to look like "volume does nothing")."""
    vol = try_read_int(args.get("volume"))
    muted = try_read_bool(args.get("muted"))
    vol = current_volume if vol is None else max(0, min(100, vol))
    return vol, current_muted if muted is None else muted


def parse_seek(args: dict) -> Tuple[Optional[float], Optional[float]]:
    """(absolute position, relative seconds) from `seek` args."""
    def num(v):
        if isinstance(v, bool) or v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return num(args.get("position")), num(args.get("seconds"))

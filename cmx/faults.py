"""
The heartbeat's `error` line: what is wrong with this screen right now.

Port of DVSI Plus `Cmx/PlayerFaults.cs`. A fault counts once it has lasted
10 s (a blip raises no alert); the line is the most important one plus how
many others there are.
"""

import time
from typing import Dict, Optional, Tuple

CREDENTIAL = "credential"
NOTHING_PLAYS = "nothing_plays"
RENDERER = "renderer"
PLAYLIST_FETCH = "playlist_fetch"
DOWNLOADS = "downloads"
CACHE_FULL = "cache_full"
LIVE_SIGNAL = "live_signal"
ITEMS_FAIL = "items_fail"
PORT = "port"

ORDER = [CREDENTIAL, NOTHING_PLAYS, RENDERER, PLAYLIST_FETCH, DOWNLOADS,
         CACHE_FULL, LIVE_SIGNAL, ITEMS_FAIL, PORT]

DEFAULT_HOLD = 10.0


class PlayerFaults:
    def __init__(self, clock=time.monotonic, hold: float = DEFAULT_HOLD):
        self._clock = clock
        self.hold = hold
        self._faults: Dict[str, Tuple[str, float]] = {}

    def set(self, code: str, message: str) -> None:
        if not message or not message.strip():
            self.clear(code)
            return
        since = self._faults[code][1] if code in self._faults else self._clock()
        self._faults[code] = (message.strip(), since)

    def clear(self, code: str) -> None:
        self._faults.pop(code, None)

    def put(self, code: str, message: Optional[str]) -> None:
        if message:
            self.set(code, message)
        else:
            self.clear(code)

    def has(self, code: str) -> bool:
        return code in self._faults

    def top(self) -> Optional[str]:
        now = self._clock()
        held = [(c, m) for c, (m, since) in self._faults.items() if now - since >= self.hold]
        # "Nothing plays" already says every item fails.
        if any(c == NOTHING_PLAYS for c, _ in held):
            held = [(c, m) for c, m in held if c != ITEMS_FAIL]
        if not held:
            return None

        def rank(code: str) -> int:
            return ORDER.index(code) if code in ORDER else len(ORDER)

        held.sort(key=lambda cm: (rank(cm[0]), cm[0]))
        first = held[0][1]
        return first if len(held) == 1 else f"{first} (+{len(held) - 1} more)"


# -- Wording, kept here so the tests pin what an operator reads -------------

def nothing_plays_message(total: int, why: str) -> str:
    return f"Nothing in the playlist plays: all {total} item(s) failed ({why}); showing the idle screen"


def items_fail_message(failed: int, total: int, example: str) -> str:
    return f"{failed} of {total} item(s) cannot be played (including {example})"


def playlist_fetch_message(failures: int, why: str, offline_copy: bool) -> str:
    return (f"The playlist could not be fetched from the CMS {failures} times in a row ({why}); "
            + ("playing the copy saved offline" if offline_copy else "nothing saved offline to play"))


def cache_full_message(gb: int) -> str:
    return f"The content cache is full ({gb} GB): new content cannot be downloaded"


def port_message(port: int, why: str) -> str:
    return f"The local player API could not listen on port {port} ({why})"


def renderer_message(why: str) -> str:
    return f"The page renderer could not be started ({why})"

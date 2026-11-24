"""
The wall's screen side: the ZoneHost the interaction engine drives, over the
renderer page. It tracks which surface is busy / playing what, turns engine
decisions into `wall` messages for the page, and feeds the page's
zone_playing / zone_returned reports back to the engine.
"""

from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import cache as cachemod
from .interaction import FULLSCREEN, LayoutOverride, SurfaceKey, ZoneHost


def canonical_keys(zones: List[dict]) -> None:
    """One key per physical zone: a lone zone bound to a trigger is that
    trigger's zone; a shared or unwired one is known by its element id."""
    counts: Dict[int, int] = {}
    for z in zones:
        if z.get("trigger_id"):
            counts[z["trigger_id"]] = counts.get(z["trigger_id"], 0) + 1
    for z in zones:
        tid = z.get("trigger_id")
        z["surface_key"] = f"zone:{tid}" if tid and counts.get(tid) == 1 else f"el:{z.get('id')}"


class WallHost(ZoneHost):
    def __init__(self, send: Callable[[dict], None], zones: List[dict], media: Dict[str, Path],
                 paused: Callable[[], bool]):
        self._send = send
        self._media = media
        self._paused = paused
        self.engine = None
        canonical_keys(zones)
        self._zones = zones
        self._state: Dict[SurfaceKey, dict] = {}

    # -- lookups -----------------------------------------------------------

    def _key_of(self, z: dict) -> SurfaceKey:
        return SurfaceKey.from_wire(z["surface_key"])

    def expand(self, s: SurfaceKey) -> List[SurfaceKey]:
        if s.is_fullscreen:
            return [FULLSCREEN]
        if s.kind == "zone":
            return [self._key_of(z) for z in self._zones if z.get("trigger_id") == s.trigger_id]
        z = next((z for z in self._zones if z.get("id") == s.element_id), None)
        return [self._key_of(z)] if z is not None else []

    def _st(self, s: SurfaceKey) -> dict:
        return self._state.get(s) or {}

    def is_busy(self, s: SurfaceKey) -> bool:
        return bool(self._st(s).get("busy"))

    def is_playing(self, s: SurfaceKey) -> bool:
        return bool(self._st(s).get("playing"))

    def content(self, s: SurfaceKey) -> Optional[str]:
        return self._st(s).get("content")

    @property
    def any_zone_busy(self) -> bool:
        return any(v.get("busy") for v in self._state.values())

    @property
    def is_paused(self) -> bool:
        return self._paused()

    def _url(self, cid: Optional[str], path) -> tuple:
        if path is None:
            return None, None
        p = Path(path)
        return f"/media/{p.name}", "image" if cachemod.is_image_file(p) else "video"

    # -- actions -------------------------------------------------------------

    def play(self, s, cid, path, hold_s, fade_ms, fit) -> None:
        self._state[s] = {"busy": True, "playing": False, "content": cid}
        url, kind = self._url(cid, path)
        self._send({"type": "wall", "op": "play", "surface": s.wire, "url": url, "kind": kind,
                    "hold_s": hold_s, "fade_ms": fade_ms, "fit": fit})

    def restart(self, s, hold_s) -> None:
        self._send({"type": "wall", "op": "restart", "surface": s.wire, "hold_s": hold_s})

    def return_all(self) -> int:
        busy = [k for k, v in self._state.items() if v.get("busy")]
        for k in busy:
            self._state[k] = {}
            if self.engine is not None:
                self.engine.on_surface_returned(k)
        self._send({"type": "wall", "op": "return_all"})
        return len(busy)

    def ensure_fullscreen(self) -> bool:
        return True

    def set_alignment(self, on: bool, scene_name: str) -> None:
        self._send({"type": "wall", "op": "alignment", "on": on, "scene": scene_name})

    def show_scene_attractor(self, cid, path, fit, fade_ms) -> None:
        url, kind = self._url(cid, path) if cid else (None, None)
        self._send({"type": "wall", "op": "attractor", "cid": cid, "url": url, "kind": kind,
                    "fit": fit, "fade_ms": fade_ms})

    def apply_scene_layout(self, layout: Dict[str, LayoutOverride], fade_ms: int) -> None:
        overrides = {}
        for eid, o in layout.items():
            j = o.to_json()
            if o.content_id and o.content_id in self._media:
                j["contentUrl"] = f"/media/{Path(self._media[o.content_id]).name}"
            overrides[eid] = j
        self._send({"type": "wall", "op": "layout", "overrides": overrides, "fade_ms": fade_ms})

    # -- page reports -----------------------------------------------------------

    def page_playing(self, wire: str) -> None:
        s = SurfaceKey.from_wire(wire)
        st = self._state.get(s)
        if st and st.get("busy"):
            st["playing"] = True
            if self.engine is not None:
                self.engine.on_surface_playing(s)

    def page_returned(self, wire: str) -> None:
        s = SurfaceKey.from_wire(wire)
        st = self._state.get(s)
        if st and st.get("busy"):
            self._state[s] = {}
            if self.engine is not None:
                self.engine.on_surface_returned(s)

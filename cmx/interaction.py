"""
The interactive-wall runtime: a port of DVSI Plus `Cmx/InteractionEngine.cs`.

A template's `interaction` spec maps sensor trigger ids to content through a
tree of scenes. This module holds the wall's state — the active scene, when
each sensor last fired — and decides what a trigger does. It has no I/O: the
screen is a `host` (see ZoneHost), the clock is injected, so the rules are
unit-tested here and must match the CMS browser runtime exactly.

  debounce   per trigger id, from the last trigger RECEIVED; silent
  paused     a paused screen refuses triggers
  unmapped   nothing in the active scene for this id → trigger_unmapped
  content    plays once on the mapping's target: the trigger's own zone(s),
             a named zone, or the fullscreen surface; none → no_zone; not
             cached → trigger_content_missing
  several    judged surface by surface; refused only when every surface was
  busy zone  ignore: dropped · switch: other content crossfades, same content
             dropped · restart: same content restarts, other crossfades
  then       applied right after playback starts (also when content missed)
  idle       a child scene with idleTimeoutS returns to its parent once that
             long has passed with no trigger and no zone playing
  schedule   windows in which a scene is HOME; checked every 15 s, acted on
             only when home changes and no zone is busy
  back       a history (cap 20) of scenes the wall came from
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

DEFAULT_IMAGE_HOLD_S = 10
SCHEDULE_CHECK_S = 15
HISTORY_CAP = 20
_IMAGE_PATH = re.compile(r"\.(png|jpe?g|gif|webp|bmp|svg)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SurfaceKey:
    kind: str                 # "zone" | "element" | "fullscreen"
    trigger_id: int = 0
    element_id: Optional[str] = None

    @staticmethod
    def zone(tid: int) -> "SurfaceKey":
        return SurfaceKey("zone", tid, None)

    @staticmethod
    def element(eid: str) -> "SurfaceKey":
        return SurfaceKey("element", 0, eid)

    @property
    def is_fullscreen(self) -> bool:
        return self.kind == "fullscreen"

    def __str__(self) -> str:
        if self.kind == "zone":
            return f"zone {self.trigger_id}"
        if self.kind == "element":
            return f"zone {self.element_id}"
        return "fullscreen"

    def to_json(self) -> dict:
        if self.kind == "zone":
            return {"kind": "zone"}
        if self.kind == "element":
            return {"kind": "zone", "elementId": self.element_id}
        return {"kind": "fullscreen"}

    @property
    def wire(self) -> str:
        """The key as the page knows it."""
        if self.kind == "zone":
            return f"zone:{self.trigger_id}"
        if self.kind == "element":
            return f"el:{self.element_id}"
        return "fullscreen"

    @staticmethod
    def from_wire(s: str) -> "SurfaceKey":
        if s.startswith("zone:"):
            return SurfaceKey.zone(int(s[5:]))
        if s.startswith("el:"):
            return SurfaceKey.element(s[3:])
        return FULLSCREEN


FULLSCREEN = SurfaceKey("fullscreen")


@dataclass
class Policy:
    retrigger: str = "restart"
    debounce_ms: int = 300
    fade_ms: int = 300
    touch: bool = False


@dataclass
class Target:
    kind: str = "zone"
    element_id: Optional[str] = None
    fit: str = "fit"

    @property
    def is_fullscreen(self) -> bool:
        return self.kind == "fullscreen"


@dataclass
class Mapping:
    trigger_id: int
    content_id: Optional[str] = None
    duration_s: Optional[float] = None
    then: Optional[str] = None
    then_stop: bool = False
    target: Optional[Target] = None


@dataclass
class LayoutOverride:
    x: Optional[float] = None
    y: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None
    visible: Optional[bool] = None
    content: Optional[str] = None
    content_id: Optional[str] = None

    @property
    def hidden(self) -> bool:
        return self.visible is False

    def to_json(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height,
                "visible": self.visible, "content": self.content, "contentId": self.content_id}


@dataclass
class Attractor:
    content_id: Optional[str] = None
    fit: str = "fill"


@dataclass
class Scene:
    id: str
    name: str = ""
    parent_id: Optional[str] = None
    idle_timeout_s: Optional[float] = None
    mappings: Dict[int, Mapping] = field(default_factory=dict)
    attractor: Optional[Attractor] = None
    layout: Dict[str, LayoutOverride] = field(default_factory=dict)


@dataclass
class ScheduleEntry:
    scene_id: str
    days: set
    start_min: int
    end_min: int

    def is_open(self, day: int, minutes: int) -> bool:
        if self.start_min < self.end_min:
            return day in self.days and self.start_min <= minutes < self.end_min
        # past midnight: open from start on a listed day, until end the morning after
        return (day in self.days and minutes >= self.start_min) or \
               (((day + 6) % 7) in self.days and minutes < self.end_min)


def _int(v, default: int) -> int:
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v))
        except ValueError:
            return default
    return default


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _minutes(v) -> Optional[int]:
    if not isinstance(v, str):
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", v.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 24 or mi > 59 or (h == 24 and mi):
        return None
    return h * 60 + mi


@dataclass
class Model:
    root_scene_id: str = ""
    policy: Policy = field(default_factory=Policy)
    scenes: Dict[str, Scene] = field(default_factory=dict)
    trigger_labels: Dict[int, str] = field(default_factory=dict)
    schedule: List[ScheduleEntry] = field(default_factory=list)

    @property
    def has_fullscreen_target(self) -> bool:
        return any(m.target is not None and m.target.is_fullscreen
                   for s in self.scenes.values() for m in s.mappings.values())

    def content_ids(self) -> set:
        out = set()
        for s in self.scenes.values():
            if s.attractor and s.attractor.content_id:
                out.add(s.attractor.content_id)
            for m in s.mappings.values():
                if m.content_id:
                    out.add(m.content_id)
            for o in s.layout.values():
                if o.content_id:
                    out.add(o.content_id)
        return out

    @staticmethod
    def parse(j, log: Callable[[str], None] = lambda s: None) -> Optional["Model"]:
        """None when there is nothing to run. Tolerant of odd fields: a wall
        that half-works beats a template that will not show."""
        if not isinstance(j, dict):
            return None
        m = Model()
        try:
            p = j.get("policy")
            if isinstance(p, dict):
                if p.get("retrigger") in ("restart", "switch", "ignore"):
                    m.policy.retrigger = p["retrigger"]
                m.policy.debounce_ms = max(0, min(10000, _int(p.get("debounceMs"), 300)))
                m.policy.fade_ms = max(0, min(5000, _int(p.get("fadeMs"), 300)))
                m.policy.touch = p.get("touch") is True
            for t in j.get("triggers") or []:
                if isinstance(t, dict):
                    tid = _int(t.get("id"), 0)
                    if tid > 0:
                        m.trigger_labels[tid] = str(t.get("label") or "")
            for s in j.get("scenes") or []:
                if not isinstance(s, dict) or not s.get("id"):
                    continue
                sid = str(s["id"])
                idle = s.get("idleTimeoutS")
                scene = Scene(id=sid, name=str(s.get("name") or sid),
                              parent_id=s.get("parentId") if isinstance(s.get("parentId"), str) else None,
                              idle_timeout_s=max(0.0, float(idle)) if _num(idle) is not None else None)
                a = s.get("attractor")
                if isinstance(a, dict):
                    cid = a.get("contentId") if isinstance(a.get("contentId"), str) and a.get("contentId") else None
                    fit = a.get("fit") if a.get("fit") in ("fill", "fit", "stretch") else "fill"
                    scene.attractor = Attractor(cid, fit)
                for mp in s.get("mappings") or []:
                    if not isinstance(mp, dict):
                        continue
                    tid = _int(mp.get("triggerId"), 0)
                    if tid <= 0 or tid in scene.mappings:
                        continue
                    then = mp.get("then") if isinstance(mp.get("then"), dict) else {}
                    scene.mappings[tid] = Mapping(
                        trigger_id=tid,
                        content_id=mp.get("contentId") if isinstance(mp.get("contentId"), str) else None,
                        duration_s=_num(mp.get("durationS")),
                        then=then.get("scene") if isinstance(then.get("scene"), str) else None,
                        then_stop=then.get("stop") is True,
                        target=Model._target(mp.get("target")),
                    )
                lo = s.get("layout")
                if isinstance(lo, dict):
                    for eid, ov in lo.items():
                        if not isinstance(ov, dict) or not eid or len(eid) > 64:
                            continue
                        w, h = _num(ov.get("width")), _num(ov.get("height"))
                        ct, ci = ov.get("content"), ov.get("contentId")
                        entry = LayoutOverride(
                            x=_num(ov.get("x")), y=_num(ov.get("y")),
                            width=w if w and w > 0 else None, height=h if h and h > 0 else None,
                            visible=ov.get("visible") if isinstance(ov.get("visible"), bool) else None,
                            content=ct if isinstance(ct, str) and 0 < len(ct) <= 2000 else None,
                            content_id=ci if isinstance(ci, str) and 0 < len(ci) <= 64 else None)
                        if any(v is not None for v in (entry.x, entry.y, entry.width, entry.height,
                                                       entry.visible, entry.content, entry.content_id)):
                            scene.layout[eid] = entry
                m.scenes[sid] = scene
            m.root_scene_id = str(j.get("rootSceneId") or "")
            if m.root_scene_id not in m.scenes:
                m.root_scene_id = next((s.id for s in m.scenes.values() if s.parent_id is None), "")
            if not m.root_scene_id:
                log("interaction spec has no usable root scene")
                return None
            for s in m.scenes.values():
                if s.id == m.root_scene_id:
                    s.parent_id = None
                elif s.parent_id is None or s.parent_id not in m.scenes:
                    s.parent_id = m.root_scene_id
            root = m.scenes[m.root_scene_id]
            if root.layout:
                log(f"root scene '{root.id}' carries a layout; the root is the template's own layout, ignored")
                root.layout.clear()
            for w in j.get("schedule") or []:
                if not isinstance(w, dict):
                    continue
                sid = w.get("sceneId")
                if not isinstance(sid, str) or sid not in m.scenes:
                    continue
                start, end = _minutes(w.get("start")), _minutes(w.get("end"))
                if start is None or end is None or start == end:
                    continue
                days = {d for d in (_int(x, -1) for x in w.get("days") or []) if 0 <= d <= 6}
                if days:
                    m.schedule.append(ScheduleEntry(sid, days, start, end))
            return m
        except Exception as e:  # pragma: no cover - defensive, like the C#
            log(f"interaction spec unreadable: {e}")
            return None

    @staticmethod
    def _target(t) -> Optional[Target]:
        if not isinstance(t, dict):
            return None
        kind = t.get("kind")
        if kind not in ("zone", "fullscreen"):
            return None
        if kind == "fullscreen":
            fit = t.get("fit") if t.get("fit") in ("fill", "fit", "stretch") else "fit"
            return Target("fullscreen", None, fit)
        eid = t.get("elementId") if isinstance(t.get("elementId"), str) and t.get("elementId") else None
        return Target("zone", eid, "fit")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ZoneHost:
    """What the engine needs from the screen (implemented by player.WallHost)."""

    def expand(self, s: SurfaceKey) -> List[SurfaceKey]: ...
    def is_busy(self, s: SurfaceKey) -> bool: ...
    def is_playing(self, s: SurfaceKey) -> bool: ...
    def content(self, s: SurfaceKey) -> Optional[str]: ...
    @property
    def any_zone_busy(self) -> bool: ...
    @property
    def is_paused(self) -> bool: ...
    def play(self, s: SurfaceKey, cid: str, path, hold_s: Optional[float], fade_ms: int, fit: Optional[str]) -> None: ...
    def restart(self, s: SurfaceKey, hold_s: Optional[float]) -> None: ...
    def return_all(self) -> int: ...
    def ensure_fullscreen(self) -> bool: ...
    def set_alignment(self, on: bool, scene_name: str) -> None: ...
    def show_scene_attractor(self, cid: Optional[str], path, fit: str, fade_ms: int) -> None: ...
    def apply_scene_layout(self, layout: Dict[str, LayoutOverride], fade_ms: int) -> None: ...


def resolve_zone(name: Optional[str], log: Callable[[str], None] = lambda s: None):
    if not name or not str(name).strip() or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(str(name).strip())
    except Exception:
        log(f"timezone '{name}' is not known on this machine; reading the wall's schedule on its own clock")
        return None


class InteractionEngine:
    def __init__(self, model: Model, media: Dict[str, object], host: ZoneHost,
                 clock: Callable[[], datetime], zone=None,
                 emit: Callable[[str, str, str, dict], None] = lambda *a: None,
                 log: Callable[[str], None] = lambda s: None,
                 exists: Callable[[object], bool] = lambda p: p is not None and getattr(p, "exists", lambda: True)()):
        self.m = model
        self._media = media
        self._host = host
        self._clock = clock
        self._zone = zone
        self._emit = emit
        self._log = log
        self._exists = exists
        self._last_trigger_at: Dict[int, datetime] = {}
        self._last_emit: Dict[str, Tuple[datetime, int]] = {}
        self._last_activity = clock()
        self._last_schedule_check = self._last_activity
        self._pending_home: Optional[str] = None
        self._attractor_key: Optional[str] = None
        self._attractor_applied = False
        self._attractor_waiting: Optional[str] = None
        self._playing: Dict[SurfaceKey, Tuple[int, str]] = {}
        self.history: List[str] = []
        self.alignment_on = False
        self.current_home = self.home()
        self.active_scene_id = self.current_home

    def start(self) -> None:
        self._apply_scene_attractor(self.active_scene_id)
        self._host.apply_scene_layout(self.scene.layout, 0)

    @property
    def scene(self) -> Scene:
        return self.m.scenes.get(self.active_scene_id) or self.m.scenes[self.m.root_scene_id]

    @property
    def _scene_name(self) -> str:
        s = self.scene
        return "'" + (s.name or s.id) + "'"

    def _cached(self, cid: Optional[str]):
        if not cid or cid not in self._media:
            return None
        p = self._media[cid]
        return p if self._exists(p) else None

    # -- commands --------------------------------------------------------------

    def trigger(self, tid: int, source: str) -> Tuple[bool, str]:
        now = self._clock()
        last = self._last_trigger_at.get(tid)
        bounced = last is not None and (now - last).total_seconds() * 1000 < self.m.policy.debounce_ms
        self._last_trigger_at[tid] = now
        if bounced:
            return False, f"trigger {tid} debounced"
        if self._host.is_paused:
            return False, "the screen is paused"

        mp = self.scene.mappings.get(tid)
        if mp is None:
            self._emit_event("warn", "trigger_unmapped", f"trigger {tid} is not mapped in scene {self._scene_name}",
                             {"trigger_id": tid, "scene": self.active_scene_id, "reason": "unmapped", "source": source})
            return False, f"trigger {tid} is not mapped in scene {self._scene_name}"
        self._emit_event("info", "trigger_received", f"trigger {tid} from {source}",
                         {"trigger_id": tid, "scene": self.active_scene_id, "source": source})

        outcome = "no content"
        if mp.then_stop:
            outcome = f"stopped {self._host.return_all()}"
        if mp.content_id:
            cid = mp.content_id
            want = self._surface_for(tid, mp.target)
            surfaces = self._host.expand(want)
            path = self._cached(cid)
            if not surfaces:
                self._emit_event("warn", "trigger_unmapped", f"no {want} answers trigger {tid}",
                                 {"trigger_id": tid, "scene": self.active_scene_id, "reason": "no_zone",
                                  "target": want.to_json()})
                outcome = f"no zone for trigger {tid}"
            elif path is None:
                self._emit_event("warn", "trigger_content_missing", f"content {cid} is not cached on this player",
                                 {"trigger_id": tid, "content_id": cid, "scene": self.active_scene_id})
                outcome = f"content {cid} is not cached on this player"
            else:
                first = refused = None
                played = 0
                for s in surfaces:
                    if self._host.is_busy(s):
                        same = self._host.content(s) == cid
                        if self.m.policy.retrigger == "ignore":
                            refused = refused or f"{s} busy (policy: ignore)"
                            continue
                        if self.m.policy.retrigger == "switch" and same:
                            refused = refused or f"{s} already playing {cid}"
                            continue
                        if self.m.policy.retrigger == "restart" and same and self._host.is_playing(s):
                            self._host.restart(s, self._hold_for(mp, path))
                            o = "restarted"
                        else:
                            self._play(s, tid, mp, cid, path)
                            o = "switched"
                    else:
                        self._play(s, tid, mp, cid, path)
                        o = f"playing {cid}"
                    first = first or o
                    played += 1
                if played == 0:
                    return False, refused
                outcome = first
                if len(surfaces) > 1:
                    outcome += f" in {played} zones" if played == len(surfaces) else f" in {played} of {len(surfaces)} zones"

        if mp.then:
            self._go(mp.then, "mapping", tid)
        self._last_activity = self._clock()
        return True, f"trigger {tid}: {outcome} (scene {self._scene_name})"

    def set_scene(self, target: str, source: str, stop: bool = False) -> Tuple[bool, str]:
        if self._resolve(target) is None:
            return False, f"unknown scene '{target}'"
        stopped = self._host.return_all() if stop else 0
        sid = self._go(target, source, None)
        return True, f"scene '{sid}'" + (f" (stopped {stopped})" if stop else "")

    def play_content(self, content_id: Optional[str], source: str) -> Tuple[bool, str]:
        cid = (content_id or "").strip()
        if not cid:
            return False, "play needs a 'content_id'"
        if self._host.is_paused:
            return False, "the screen is paused"
        if cid not in self._media:
            return False, f"content {cid} is not part of this wall"
        path = self._cached(cid)
        if path is None:
            return False, f"content {cid} is not cached on this player"
        if not self._host.ensure_fullscreen():
            return False, "this wall has no fullscreen surface"
        like = next((mp for s in self.m.scenes.values() for mp in s.mappings.values()
                     if mp.content_id == cid and mp.target is not None and mp.target.is_fullscreen), None)
        mp = Mapping(trigger_id=0, content_id=cid, duration_s=like.duration_s if like else None,
                     target=Target("fullscreen", None, like.target.fit if like else "fit"))
        s = FULLSCREEN
        if self._host.is_busy(s):
            same = self._host.content(s) == cid
            if self.m.policy.retrigger == "ignore":
                return False, "fullscreen busy (policy: ignore)"
            if self.m.policy.retrigger == "switch" and same:
                return False, f"fullscreen already playing {cid}"
            if self.m.policy.retrigger == "restart" and same and self._host.is_playing(s):
                self._host.restart(s, self._hold_for(mp, path))
                outcome = "restarted"
            else:
                self._play(s, 0, mp, cid, path)
                outcome = "switched"
        else:
            self._play(s, 0, mp, cid, path)
            outcome = f"playing {cid}"
        self._emit_event("info", "content_requested", f"content {cid} from {source}",
                         {"content_id": cid, "scene": self.active_scene_id, "source": source})
        self._last_activity = self._clock()
        return True, f"{outcome} (scene {self._scene_name})"

    def set_alignment(self, on: bool) -> Tuple[bool, str]:
        self.alignment_on = on
        self._host.set_alignment(on, self.scene.name)
        return True, f"alignment overlay {'on' if on else 'off'}"

    def reset(self) -> None:
        self._last_trigger_at.clear()
        self.current_home = self.home()
        self._pending_home = None
        self._last_schedule_check = self._clock()
        self.history.clear()
        self._switch_scene(self.current_home, "reset", None)
        self._host.apply_scene_layout(self.scene.layout, self.m.policy.fade_ms)
        self._playing.clear()

    # -- host callbacks ----------------------------------------------------------

    def on_surface_playing(self, s: SurfaceKey) -> None:
        p = self._playing.get(s)
        if p is None:
            return
        detail = {"trigger_id": p[0], "content_id": p[1], "scene": self.active_scene_id}
        if s.is_fullscreen:
            detail["target"] = "fullscreen"
        self._emit_event("info", "zone_playing", f"{s} plays {p[1]} (trigger {p[0]})", detail, f"zone_playing:{s}")

    def on_surface_returned(self, s: SurfaceKey) -> None:
        self._last_activity = self._clock()
        p = self._playing.pop(s, None)
        if p is None:
            return
        detail = {"trigger_id": p[0], "content_id": p[1]}
        if s.is_fullscreen:
            detail["target"] = "fullscreen"
        self._emit_event("info", "zone_ended", f"{s} ended {p[1]} (trigger {p[0]})", detail, f"zone_ended:{s}")

    def tick(self) -> None:
        now = self._clock()
        if self.m.schedule and (now - self._last_schedule_check).total_seconds() >= SCHEDULE_CHECK_S:
            self._last_schedule_check = now
            self._check_schedule()
        if self._attractor_waiting is not None and self._cached(self._attractor_waiting) is not None:
            self._apply_scene_attractor(self.active_scene_id)
        if self._host.is_paused:
            return
        if self._pending_home is not None and not self._host.any_zone_busy:
            target, self._pending_home = self._pending_home, None
            if self.active_scene_id != target:
                self._switch_scene(target, "schedule", None)
            return
        if self.active_scene_id == self.current_home:
            return
        s = self.scene
        if s.parent_id is None or s.idle_timeout_s is None or self._host.any_zone_busy:
            return
        if (now - self._last_activity).total_seconds() >= s.idle_timeout_s:
            self._switch_scene(s.parent_id, "idle", None)

    # -- schedule ----------------------------------------------------------------

    def _local_now(self) -> Tuple[int, int]:
        utc = self._clock()
        if utc.tzinfo is None:
            utc = utc.replace(tzinfo=timezone.utc)
        local = utc.astimezone(self._zone) if self._zone is not None else utc.astimezone()
        return local.weekday(), local.hour * 60 + local.minute

    def home(self) -> str:
        if self.m.schedule:
            day, minutes = self._local_now()
            for w in self.m.schedule:
                if w.scene_id in self.m.scenes and w.is_open(day, minutes):
                    return w.scene_id
        return self.m.root_scene_id

    def _check_schedule(self) -> None:
        h = self.home()
        if h == self.current_home:
            return
        self._emit_event("info", "home_changed", f"home {self.current_home} -> {h}",
                         {"from": self.current_home, "to": h})
        self.current_home = h
        self._pending_home = h

    # -- attractor ----------------------------------------------------------------

    def _apply_scene_attractor(self, scene_id: str) -> None:
        key, path, fit = self._scene_attractor_key(scene_id)
        if self._attractor_applied and key == self._attractor_key:
            return
        self._attractor_applied = True
        self._attractor_key = key
        self._host.show_scene_attractor(key, path, fit, self.m.policy.fade_ms)

    def _scene_attractor_key(self, scene_id: str):
        self._attractor_waiting = None
        sc = self.m.scenes.get(scene_id)
        if sc is None or sc.attractor is None:
            return None, None, "fill"
        fit = sc.attractor.fit
        cid = sc.attractor.content_id
        if not cid:
            return "", None, fit
        path = self._cached(cid)
        if path is None:
            self._emit_event("warn", "attractor_content_missing", f"scene attractor {cid} is not cached on this player",
                             {"content_id": cid, "scene": scene_id})
            if cid in self._media:
                self._attractor_waiting = cid
            return None, None, fit
        return cid, path, fit

    # -- moving -------------------------------------------------------------------

    def _hold_for(self, mp: Mapping, path) -> Optional[float]:
        if _IMAGE_PATH.search(str(path)):
            return mp.duration_s if mp.duration_s and mp.duration_s > 0 else DEFAULT_IMAGE_HOLD_S
        return None

    @staticmethod
    def _surface_for(tid: int, t: Optional[Target]) -> SurfaceKey:
        if t is not None and t.is_fullscreen:
            return FULLSCREEN
        if t is not None and t.element_id:
            return SurfaceKey.element(t.element_id)
        return SurfaceKey.zone(tid)

    def _play(self, s: SurfaceKey, tid: int, mp: Mapping, cid: str, path) -> None:
        self._playing[s] = (tid, cid)
        self._host.play(s, cid, path, self._hold_for(mp, path), self.m.policy.fade_ms,
                        mp.target.fit if mp.target is not None and mp.target.is_fullscreen else None)

    def _back_target(self) -> str:
        while self.history:
            top = self.history[-1]
            if top in self.m.scenes and top != self.active_scene_id:
                return top
            self.history.pop()
        return self.current_home

    def _resolve(self, target: str) -> Optional[str]:
        if target == "root":
            return self.current_home
        if target == "parent":
            return self.scene.parent_id
        if target == "back":
            return self._back_target()
        return target if target in self.m.scenes else None

    def _go(self, target: str, cause: str, tid: Optional[int]) -> Optional[str]:
        sid = self._resolve(target)
        if sid is None:
            return None
        is_back = target == "back"
        if is_back and self.history and self.history[-1] == sid:
            self.history.pop()
        self._switch_scene(sid, cause, tid, is_back)
        return sid

    def _switch_scene(self, scene_id: str, cause: str, tid: Optional[int], is_back: bool = False) -> None:
        if scene_id not in self.m.scenes:
            return
        frm = self.active_scene_id
        if frm != scene_id:
            if scene_id == self.current_home:
                self.history.clear()
            elif not is_back and cause not in ("idle", "schedule", "reset") and \
                    (not self.history or self.history[-1] != frm):
                self.history.append(frm)
                if len(self.history) > HISTORY_CAP:
                    self.history.pop(0)
        self.active_scene_id = scene_id
        self._last_activity = self._clock()
        self._apply_scene_attractor(scene_id)
        if frm == scene_id:
            return
        self._host.apply_scene_layout(self.scene.layout, self.m.policy.fade_ms)
        if self.alignment_on:
            self._host.set_alignment(True, self.scene.name)
        detail = {"from": frm, "to": scene_id, "cause": cause}
        if tid is not None:
            detail["trigger_id"] = tid
        self._emit_event("info", "scene_changed", f"scene {frm} -> {scene_id} ({cause})", detail)

    def _emit_event(self, level: str, code: str, message: str, detail: dict, limit_key: Optional[str] = None) -> None:
        """At most one event per code (or surface) per second; the next one
        carries how many were held back."""
        now = self._clock()
        key = limit_key or code
        le = self._last_emit.get(key)
        if le is not None and (now - le[0]).total_seconds() * 1000 < 1000:
            self._last_emit[key] = (le[0], le[1] + 1)
            self._log(f"[wall] {code}: {message} (rate-limited)")
            return
        if le is not None and le[1] > 0:
            detail["suppressed"] = le[1]
        self._last_emit[key] = (now, 0)
        self._log(f"[wall] {code}: {message}")
        try:
            self._emit(level, code, message, detail)
        except Exception:
            pass

    def snapshot(self) -> dict:
        day, minutes = self._local_now()
        return {
            "interactive": True, "active_scene": self.active_scene_id, "home": self.current_home,
            "history": list(self.history), "attractor": self._attractor_key, "alignment": self.alignment_on,
            "policy": {"retrigger": self.m.policy.retrigger, "debounceMs": self.m.policy.debounce_ms,
                       "fadeMs": self.m.policy.fade_ms, "touch": self.m.policy.touch},
            "clock": f"day {day} {minutes // 60:02d}:{minutes % 60:02d}",
        }


def wall_command_refusal(args: dict, template_on_screen: Optional[str]) -> Optional[str]:
    """DVSI Plus WallCommandTarget: a command naming another template than
    the one on screen is refused."""
    tid = args.get("template_id")
    if isinstance(tid, str) and tid and template_on_screen and tid != template_on_screen:
        return f"template {tid} is not on screen"
    return None

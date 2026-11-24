"""The wall rules, against a fake screen and clock (mirrors the C# harness)."""

from datetime import datetime, timedelta, timezone

from cmx.interaction import FULLSCREEN, InteractionEngine, Model, SurfaceKey, wall_command_refusal


class FakeHost:
    def __init__(self, zones):
        self.zones = zones                # tid -> [SurfaceKey]
        self.state = {}
        self.paused = False
        self.calls = []

    def expand(self, s):
        if s.is_fullscreen:
            return [FULLSCREEN]
        if s.kind == "zone":
            return list(self.zones.get(s.trigger_id, []))
        return [k for ks in self.zones.values() for k in ks if k.element_id == s.element_id]

    def is_busy(self, s):
        return s in self.state

    def is_playing(self, s):
        return s in self.state

    def content(self, s):
        return self.state.get(s)

    @property
    def any_zone_busy(self):
        return bool(self.state)

    @property
    def is_paused(self):
        return self.paused

    def play(self, s, cid, path, hold, fade, fit):
        self.state[s] = cid
        self.calls.append(("play", str(s), cid, hold, fit))

    def restart(self, s, hold):
        self.calls.append(("restart", str(s)))

    def return_all(self):
        n = len(self.state)
        self.state.clear()
        return n

    def ensure_fullscreen(self):
        return True

    def set_alignment(self, on, name):
        self.calls.append(("align", on, name))

    def show_scene_attractor(self, cid, path, fit, fade):
        self.calls.append(("attractor", cid, fit))

    def apply_scene_layout(self, layout, fade):
        self.calls.append(("layout", sorted(layout)))


SPEC = {
    "policy": {"retrigger": "restart", "debounceMs": 300, "fadeMs": 200, "touch": True},
    "rootSceneId": "home",
    "scenes": [
        {"id": "home", "name": "Home", "mappings": [
            {"triggerId": 1, "contentId": "clipA", "then": {"scene": "menu"}},
            {"triggerId": 2, "contentId": "pic", "durationS": 4},
            {"triggerId": 3, "contentId": "big", "target": {"kind": "fullscreen", "fit": "fill"}},
            {"triggerId": 4, "contentId": "missing"},
        ]},
        {"id": "menu", "name": "Menu", "parentId": "home", "idleTimeoutS": 30,
         "attractor": {"contentId": "loop", "fit": "fit"},
         "layout": {"z1": {"x": 10, "visible": False}},
         "mappings": [{"triggerId": 1, "contentId": "clipB", "then": {"scene": "back", "stop": True}}]},
        {"id": "night", "name": "Night"},
    ],
    "schedule": [{"sceneId": "night", "days": [0, 1, 2, 3, 4, 5, 6], "start": "22:00", "end": "06:00"}],
}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += timedelta(seconds=s)


def make(tmp_path, at=datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)):
    media = {}
    for cid, ext in (("clipA", ".mp4"), ("clipB", ".mp4"), ("pic", ".png"), ("big", ".mp4"), ("loop", ".mp4")):
        p = tmp_path / f"{cid}{ext}"
        p.write_bytes(b"x")
        media[cid] = p
    media["missing"] = tmp_path / "missing.mp4"        # listed, not on disk
    host = FakeHost({1: [SurfaceKey.zone(1)], 2: [SurfaceKey.zone(2)], 4: [SurfaceKey.zone(4)]})
    events = []
    clock = Clock(at)
    eng = InteractionEngine(Model.parse(SPEC), media, host, clock, zone=timezone.utc,
                            emit=lambda *a: events.append(a[1]))
    eng.start()
    return eng, host, clock, events


def test_parse_reads_policy_scenes_and_schedule():
    m = Model.parse(SPEC)
    assert m.policy.touch and m.policy.fade_ms == 200
    assert m.scenes["menu"].parent_id == "home" and m.scenes["night"].parent_id == "home"
    assert m.has_fullscreen_target
    assert m.content_ids() == {"clipA", "clipB", "pic", "big", "missing", "loop"}
    assert Model.parse({"scenes": []}) is None


def test_trigger_plays_then_moves_and_attractor_follows(tmp_path):
    eng, host, clock, events = make(tmp_path)
    ok, why = eng.trigger(1, "cms")
    assert ok and why == "trigger 1: playing clipA (scene 'Menu')"
    assert eng.active_scene_id == "menu"
    assert ("attractor", "loop", "fit") in host.calls
    assert ("layout", ["z1"]) in host.calls
    assert events[:2] == ["trigger_received", "scene_changed"]


def test_debounce_unmapped_paused_and_missing(tmp_path):
    eng, host, clock, events = make(tmp_path)
    eng.trigger(2, "cms")
    assert eng.trigger(2, "cms") == (False, "trigger 2 debounced")
    clock.advance(1)
    assert eng.trigger(9, "cms") == (False, "trigger 9 is not mapped in scene 'Home'")
    ok, why = eng.trigger(4, "cms")
    assert ok and "content missing is not cached on this player" in why
    host.paused = True
    clock.advance(1)
    assert eng.trigger(2, "cms") == (False, "the screen is paused")


def test_image_hold_and_fullscreen_fit(tmp_path):
    eng, host, clock, _ = make(tmp_path)
    eng.trigger(2, "cms")
    eng.trigger(3, "cms")
    assert ("play", "zone 2", "pic", 4, None) in host.calls
    assert ("play", "fullscreen", "big", None, "fill") in host.calls


def test_retrigger_restart_same_content(tmp_path):
    eng, host, clock, _ = make(tmp_path)
    eng.trigger(2, "cms")
    clock.advance(1)
    ok, why = eng.trigger(2, "cms")
    assert ok and why.startswith("trigger 2: restarted")


def test_then_stop_and_back_history(tmp_path):
    eng, host, clock, _ = make(tmp_path)
    eng.trigger(1, "cms")                      # home -> menu, history [home]
    assert eng.history == ["home"]
    clock.advance(1)
    ok, why = eng.trigger(1, "cms")            # stop everything, play clipB, then back
    assert ok and eng.active_scene_id == "home" and eng.history == []


def test_idle_returns_to_parent_only_when_nothing_plays(tmp_path):
    eng, host, clock, _ = make(tmp_path)
    eng.trigger(1, "cms")
    clock.advance(31)
    eng.tick()
    assert eng.active_scene_id == "menu"       # clipA still playing
    eng.on_surface_returned(SurfaceKey.zone(1))
    host.state.clear()
    clock.advance(31)
    eng.tick()
    assert eng.active_scene_id == "home"


def test_schedule_window_past_midnight_is_home(tmp_path):
    eng, host, clock, events = make(tmp_path, at=datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc))
    assert eng.current_home == "night" and eng.active_scene_id == "night"
    eng2, _, clock2, events2 = make(tmp_path, at=datetime(2026, 11, 23, 21, 59, 50, tzinfo=timezone.utc))
    assert eng2.active_scene_id == "home"
    clock2.advance(20)
    eng2.tick()
    assert "home_changed" in events2 and eng2.active_scene_id == "night"


def test_set_scene_play_content_and_alignment(tmp_path):
    eng, host, clock, _ = make(tmp_path)
    assert eng.set_scene("nowhere", "cms") == (False, "unknown scene 'nowhere'")
    assert eng.set_scene("menu", "cms") == (True, "scene 'menu'")
    assert eng.play_content("nope", "cms") == (False, "content nope is not part of this wall")
    assert eng.play_content("missing", "cms") == (False, "content missing is not cached on this player")
    ok, why = eng.play_content("big", "cms")
    assert ok and why.startswith("playing big")
    assert eng.set_alignment(True) == (True, "alignment overlay on")


def test_events_are_rate_limited(tmp_path):
    eng, host, clock, events = make(tmp_path)
    for i in range(5):
        clock.advance(0.35)
        eng.trigger(9, "cms")
    assert events.count("trigger_unmapped") < 5


def test_wall_command_refusal():
    assert wall_command_refusal({"template_id": "B"}, "A") == "template B is not on screen"
    assert wall_command_refusal({"template_id": "A"}, "A") is None
    assert wall_command_refusal({}, "A") is None

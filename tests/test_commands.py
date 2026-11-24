from cmx.commands import (RESEND, RUN, RUNNING, CommandGate, Coalescer, parse_seek, parse_volume,
                          try_read_bool, try_read_int)


def test_command_runs_once_and_recorded_result_is_resent():
    g = CommandGate()
    assert g.claim("c1") == (RUN, None)
    assert g.claim("c1") == (RUNNING, None)         # still executing
    g.record("c1", True, "seeked to 10s of 60s")
    assert g.claim("c1") == (RESEND, (True, "seeked to 10s of 60s"))


def test_gate_forgets_beyond_its_window():
    g = CommandGate(window=3)
    for i in range(4):
        g.claim(i)
        g.record(i, True, "ok")
    assert g.claim(0) == (RUN, None)               # evicted, so new again
    assert g.claim(3)[0] == RESEND


def test_next_previous_coalesced_but_play_is_not():
    now = [100.0]
    c = Coalescer(clock=lambda: now[0])
    assert c.should_run("next")
    assert not c.should_run("next")                # heartbeat + SSE double delivery
    now[0] += 1.5
    assert c.should_run("next")
    assert c.should_run("play") and c.should_run("play")


def test_volume_parsing_accepts_strings_and_keeps_missing_values():
    assert parse_volume({"volume": "40", "muted": "true"}, 100, False) == (40, True)
    assert parse_volume({"volume": 150}, 50, True) == (100, True)
    assert parse_volume({"volume": "loud", "muted": 0}, 70, True) == (70, False)
    assert parse_volume({}, 33, False) == (33, False)


def test_readers():
    assert try_read_int(12.6) == 13
    assert try_read_int(True) is None
    assert try_read_bool("0") is False
    assert try_read_bool(2) is True
    assert try_read_bool("maybe") is None


def test_seek_args():
    assert parse_seek({"position": "12"}) == (12.0, None)
    assert parse_seek({"seconds": -5}) == (None, -5.0)
    assert parse_seek({}) == (None, None)

"""The kiosk keeps its window over the whole screen (cmx/kiosk.py)."""

import asyncio
from pathlib import Path

from cmx import kiosk
from cmx.kiosk import Kiosk


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(fit, clock):
    k = Kiosk(":0", Path("/tmp/profile"), "http://127.0.0.1:8090/", clock=clock, fit=fit)
    k._started_at = clock.now          # as _launch sets it
    return k


def test_window_is_checked_every_few_seconds_once_the_browser_is_up():
    clock, asked = Clock(), []
    k = make(lambda url: asked.append(url) or None, clock)

    async def run():
        await k._keep_fullscreen()                  # just launched: not yet
        assert asked == []
        clock.now += kiosk.FIT_EVERY
        await k._keep_fullscreen()
        assert asked == ["http://127.0.0.1:8090/"]
        clock.now += 1
        await k._keep_fullscreen()                  # too soon for the next look
        assert len(asked) == 1
        clock.now += kiosk.FIT_EVERY
        await k._keep_fullscreen()
        assert len(asked) == 2

    asyncio.run(run())


def test_a_refit_that_does_not_stick_backs_off(caplog):
    # A setup where fullscreen never sticks must not toggle the window every 5 s.
    clock, asked = Clock(), []
    k = make(lambda url: asked.append(url) or "1050x811 on a 1600x900 screen", clock)

    async def run():
        clock.now += kiosk.FIT_EVERY
        with caplog.at_level("INFO", logger="player"):
            await k._keep_fullscreen()
        assert len(asked) == 1
        assert "1050x811 on a 1600x900 screen" in caplog.text
        clock.now += kiosk.FIT_EVERY
        await k._keep_fullscreen()                  # still wrong right after a refit
        assert len(asked) == 2
        clock.now += kiosk.FIT_EVERY
        await k._keep_fullscreen()                  # so the next try waits longer
        assert len(asked) == 2
        clock.now += kiosk.FIT_RETRY
        await k._keep_fullscreen()
        assert len(asked) == 3

    asyncio.run(run())


def test_a_refit_that_sticks_keeps_the_short_interval():
    # Fixed, then a later screen change (another RDP session, TV off and on):
    # caught at the next 5 s look, not 30 s later.
    clock, answers, asked = Clock(), ["800x500 on a 1600x900 screen", None, "800x500 on a 1600x900 screen"], []
    k = make(lambda url: asked.append(url) or answers.pop(0), clock)

    async def run():
        for _ in range(3):
            clock.now += kiosk.FIT_EVERY
            await k._keep_fullscreen()
        assert len(asked) == 3

    asyncio.run(run())


def test_a_failing_check_does_not_stop_the_kiosk():
    clock = Clock()

    def broken(url):
        raise OSError("DevTools went away")

    k = make(broken, clock)

    async def run():
        clock.now += kiosk.FIT_EVERY
        await k._keep_fullscreen()                  # no exception escapes

    asyncio.run(run())

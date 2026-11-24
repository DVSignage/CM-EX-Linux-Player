"""Shared fixtures: the repo root on sys.path and fakes for mpv and the browser."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "fixtures"

PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
MP4_HEAD = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4


class FakeMpv:
    """Records what the engine asks of mpv."""

    def __init__(self):
        self.loads = []          # (path, kwargs)
        self.generation = 0
        self.paused = False
        self.stopped = 0
        self.volume = (100, False)
        self.time_pos = None
        self.duration = None
        self.core_idle = True
        self.filter = None
        self.seeks = []
        self.osd = []
        self.props = {}
        self.eof_cb = None

    def play_file(self, path, loop=False, vf=None, start=None, end=None, paused=False):
        self.generation += 1
        self.loads.append((path, dict(loop=loop, vf=vf, start=start, end=end, paused=paused)))
        self.paused = paused
        self.filter = vf
        return self.generation

    def show_still(self, path):
        return self.play_file(path, paused=True)

    def set_filter(self, vf):
        self.filter = vf

    def seek(self, s):
        self.seeks.append(s)

    def pause(self, p):
        self.paused = p

    def stop(self):
        self.generation += 1
        self.stopped += 1

    def set_volume(self, v, m):
        self.volume = (v, m)

    def set(self, k, v):
        self.props[k] = v

    def get(self, k, default=None):
        return self.props.get(k, default)

    def display_size(self):
        return (1920, 1080)

    def set_eof_callback(self, fn):
        self.eof_cb = fn

    def screenshot(self, path):
        return False

    def show_osd(self, text, duration_ms=0):
        self.osd.append(text)

    def clear_osd(self):
        pass


class FakeBrowser:
    def __init__(self, ok=True):
        self.ok = ok
        self.shown = []
        self.url = None
        self.last_error = None

    def showing(self, url):
        return self.url is not None and self.url == url

    @property
    def running(self):
        return self.url is not None

    async def show(self, url, scale=None):
        if not self.ok:
            self.last_error = "no Chromium browser installed"
            return False
        self.shown.append((url, scale))
        self.url = url
        return True

    async def stop(self):
        self.url = None


@pytest.fixture
def fake_mpv():
    return FakeMpv()


@pytest.fixture
def fake_browser():
    return FakeBrowser()

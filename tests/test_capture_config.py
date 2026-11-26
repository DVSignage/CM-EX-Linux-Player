"""Live capture asks the camera for a bounded size (a Pi decodes every frame on the CPU)."""
from pathlib import Path

from cmx.config import Settings

STATIC = Path(__file__).resolve().parents[1] / "cmx" / "renderer" / "static"


def test_capture_max_height_default_and_clamped():
    assert Settings.from_dict({}).capture_max_height == 720
    assert Settings.from_dict({"playback": {"capture_max_height": 1080}}).capture_max_height == 1080
    assert Settings.from_dict({"playback": {"capture_max_height": 50}}).capture_max_height == 240
    assert Settings.from_dict({"playback": {"capture_max_height": 99999}}).capture_max_height == 2160
    assert Settings.from_dict({"playback": {"capture_max_height": "lots"}}).capture_max_height == 720


def test_page_asks_for_the_tile_size_not_fixed_1080p():
    js = (STATIC / "capture.js").read_text()
    assert "ideal: 1920" not in js and "ideal: 1080" not in js
    assert "wantedSize(video)" in js and "capture_max_height" in js
    assert "capture_max_height: 720" in (STATIC / "renderer.js").read_text()

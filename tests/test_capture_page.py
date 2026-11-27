"""The renderer page's camera handling and its reports to the player.

Checked in a browser (headless Chrome, fake camera) when written; these pin the
pieces so they are not lost in a later edit."""
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "cmx" / "renderer" / "static"


def test_reports_cancel_the_unread_reply():
    # an unread fetch body holds a shared-memory handle: the page froze after ~16 000
    js = (STATIC / "renderer.js").read_text()
    post = js[js.index("function post("):js.index("function showSurface")]
    assert "r.body.cancel()" in post


def test_camera_shows_without_sound_when_the_microphone_fails():
    js = (STATIC / "capture.js").read_text()
    assert "async function pickMic(cam)" in js and "groupId" in js
    assert "openMedia({ video: pic, audio: false })" in js        # the picture-only retry
    assert "showing the picture without sound" in js


def test_camera_open_gives_up_and_errors_reach_the_player_log():
    js = (STATIC / "capture.js").read_text()
    assert "OPEN_TIMEOUT_MS" in js and "TimeoutError" in js
    assert 'CMX.post("error"' in js and "if (why === lastReport) return;" in js
    player = (STATIC.parents[1] / "player.py").read_text()
    assert 'elif t == "error":' in player


def test_a_replaced_or_removed_tile_closes_its_camera():
    js = (STATIC / "template.js").read_text()
    assert "node.cmxCapture = slot" in js and "function release(node)" in js
    assert js.count("release(") >= 3          # the helper, a swapped tile, a removed tile

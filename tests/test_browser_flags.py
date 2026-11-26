"""Chromium flags that keep an unattended screen free of desktop dialogs."""
from cmx.browser import DISABLED_FEATURES, FLAGS
from cmx.kiosk import KIOSK_FLAGS


def _last_disabled(flags):
    # Chromium honours only the last --disable-features it is given
    found = [f for f in flags if f.startswith("--disable-features=")]
    return found[-1].split("=", 1)[1].split(",") if found else []


def test_camera_portal_off_in_every_window():
    assert "WebRtcPipeWireCamera" in DISABLED_FEATURES
    for flags in (FLAGS, KIOSK_FLAGS):
        assert "WebRtcPipeWireCamera" in _last_disabled(flags)
        assert "TranslateUI" in _last_disabled(flags)
    assert "MediaRouter" in _last_disabled(KIOSK_FLAGS)
    assert "--use-fake-ui-for-media-stream" in KIOSK_FLAGS

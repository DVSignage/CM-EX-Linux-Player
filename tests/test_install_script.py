"""
install.sh: the desktop display detection and the service's NoNewPrivileges.

The installer needs root and a real desktop, so its functions are lifted out
of the script and run in bash with ps / pgrep / ls / snap stubbed.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parent.parent / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# The helpers and globals detect_display() leans on, under the script's own
# strict mode: an unguarded pipeline that finds nothing ends the install there.
PRELUDE = r"""
set -euo pipefail
BOLD=""; NC=""
ok() { :; }; warn() { :; }; info() { :; }
DESKTOP_USER=dvsi; DESKTOP_UID=1000
X_DISPLAY=":0"; XAUTH_PATH=""; WAYLAND_SESSION=0
"""


def function_source(name: str) -> str:
    text = INSTALL.read_text()
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m, f"{name}() not found in install.sh"
    return m.group(0)


def run_bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def detect(stubs: str) -> subprocess.CompletedProcess:
    return run_bash(PRELUDE + stubs + function_source("detect_display")
                    + 'detect_display\necho "wayland=$WAYLAND_SESSION display=$X_DISPLAY xauth=$XAUTH_PATH"\n')


def test_wayland_session_before_xwayland_has_started():
    # GNOME 46 starts Xwayland on demand: a freshly booted Ubuntu desktop with no
    # X11 app open has the Wayland socket but no Xwayland process.
    r = detect(r"""
pgrep() { return 1; }
ls() { return 0; }
ps() { printf '/usr/bin/gnome-shell\n/usr/libexec/gsd-power\n'; }
""")
    assert r.returncode == 0, f"install stopped in detect_display: {r.stderr!r}"
    assert r.stdout.strip() == "wayland=1 display=:0 xauth="


def test_wayland_session_with_xwayland_running_takes_its_display():
    r = detect(r"""
pgrep() { return 0; }
ls() { return 1; }
ps() { printf '/usr/bin/gnome-shell\n/usr/bin/Xwayland :1 -rootless -noreset -auth /run/user/1000/.mutter-Xwaylandauth.ABC\n'; }
""")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "wayland=1 display=:1 xauth="


def snap_check(stubs: str) -> str:
    r = run_bash(PRELUDE + stubs + function_source("chromium_is_snap")
                 + "if chromium_is_snap; then echo snap; else echo not-snap; fi\n")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_snap_chromium_is_detected():
    assert snap_check('snap() { [[ "$1 $2" == "list chromium" ]]; }\n') == "snap"


def test_chromium_not_installed_as_a_snap():
    assert snap_check("snap() { return 1; }\n") == "not-snap"


def test_no_snapd_at_all():
    assert snap_check("PATH=/nonexistent\n") == "not-snap"


def test_service_no_new_privileges_follows_the_snap_check():
    # snap-confine is setuid root: under NoNewPrivileges=true it refuses to start
    # and a snap Chromium kiosk exits with code 1 at every launch.
    text = INSTALL.read_text()
    service = text[text.index('cat > "$SYSTEMD_FILE" << SERVICEEOF'):text.index("\nSERVICEEOF\n")]
    assert "NoNewPrivileges=$NO_NEW_PRIVILEGES" in service
    assert "NoNewPrivileges=true" not in service

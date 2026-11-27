"""
install.sh and offline-install.sh: desktop user and display detection, and the
service unit they write.

The installers need root and a real desktop, so their functions are lifted out
of the scripts and run in bash with ps / pgrep / ls / loginctl / snap stubbed.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["install.sh", "offline-install.sh"]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# The helpers and globals the functions lean on, under the scripts' own strict
# mode: an unguarded pipeline that finds nothing ends the install there.
PRELUDE = r"""
set -euo pipefail
BOLD=""; NC=""
ok() { :; }; warn() { :; }; info() { :; }; fail() { echo "FAIL: $*"; exit 1; }
ARG_USER=""
DESKTOP_USER=dvsi; DESKTOP_UID=1000
X_DISPLAY=":0"; XAUTH_PATH=""; WAYLAND_SESSION=0
"""

# ps as a stock GNOME-on-Wayland desktop shows it before any X11 app has opened:
# no Xorg, no Xwayland. `ps aux` also prints start times (10:37).
PS_WAYLAND = r"""
ps() {
    if [[ "${1:-}" == aux ]]; then
        printf 'dvsi 1712 0.3 1.2 10:37 0:04 /usr/bin/gnome-shell\n'
    else
        printf '/usr/bin/gnome-shell\n/usr/libexec/gsd-power\n'
    fi
}
"""


def function_source(script: str, name: str) -> str:
    text = (ROOT / script).read_text()
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m, f"{name}() not found in {script}"
    return m.group(0)


def run_bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def service_unit(script: str) -> str:
    text = (ROOT / script).read_text()
    m = re.search(r'cat > "\$SYSTEMD_FILE" <<\s*SERVICEEOF\n(.*?)\nSERVICEEOF\n', text, re.S)
    assert m, f"service unit not found in {script}"
    return m.group(1)


# --- desktop user ------------------------------------------------------------

@pytest.mark.parametrize("script", SCRIPTS)
def test_desktop_user_on_wayland_without_user_flag(script):
    # No Xorg process: the Xorg lookup finds nothing, and the graphical login
    # session has to name the user.
    r = run_bash(PRELUDE + PS_WAYLAND + r"""
DESKTOP_USER=""; DESKTOP_UID=""
loginctl() {
    case "$1" in
        list-sessions) echo "      2 1000 dvsi seat0 tty2" ;;
        show-session)  echo wayland ;;
    esac
}
id() { echo 1000; }
""" + function_source(script, "detect_desktop_user")
        + 'detect_desktop_user\necho "user=$DESKTOP_USER uid=$DESKTOP_UID"\n')
    assert r.returncode == 0, f"install stopped in detect_desktop_user: {r.stdout!r} {r.stderr!r}"
    assert r.stdout.strip().splitlines()[-1] == "user=dvsi uid=1000"


# --- display -----------------------------------------------------------------

def detect(script: str, stubs: str) -> subprocess.CompletedProcess:
    return run_bash(PRELUDE + stubs + function_source(script, "detect_display")
                    + 'detect_display\necho "wayland=$WAYLAND_SESSION display=$X_DISPLAY xauth=$XAUTH_PATH"\n')


@pytest.mark.parametrize("script", SCRIPTS)
def test_wayland_session_before_xwayland_has_started(script):
    # GNOME 46 starts Xwayland on demand: a freshly booted Ubuntu desktop with no
    # X11 app open has the Wayland socket but no Xwayland process.
    r = detect(script, PS_WAYLAND + r"""
pgrep() { return 1; }
ls() { return 0; }
""")
    assert r.returncode == 0, f"install stopped in detect_display: {r.stdout!r} {r.stderr!r}"
    assert r.stdout.strip() == "wayland=1 display=:0 xauth="


@pytest.mark.parametrize("script", SCRIPTS)
def test_wayland_session_with_xwayland_running_takes_its_display(script):
    r = detect(script, r"""
pgrep() { return 0; }
ls() { return 1; }
ps() { printf '/usr/bin/gnome-shell\n/usr/bin/Xwayland :1 -rootless -noreset -auth /run/user/1000/.mutter-Xwaylandauth.ABC\n'; }
""")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "wayland=1 display=:1 xauth="


@pytest.mark.parametrize("script", SCRIPTS)
def test_x11_display_is_read_from_xorg_arguments_not_start_time(script, tmp_path):
    # lightdm's -auth file is root-only; the desktop user's own file opens the display.
    home = tmp_path / "dvsi"
    home.mkdir()
    (home / ".Xauthority").write_text("cookie")
    r = detect(script, rf"""
pgrep() {{ return 1; }}
ls() {{ return 1; }}
ps() {{
    if [[ "${{1:-}}" == aux ]]; then
        printf 'root 702 1.1 2.0 10:37 0:20 /usr/lib/xorg/Xorg :0 -seat seat0 -auth /var/run/lightdm/root/:0 -nolisten tcp vt7\n'
    else
        printf '/usr/lib/xorg/Xorg :0 -seat seat0 -auth /var/run/lightdm/root/:0 -nolisten tcp vt7\n'
    fi
}}
getent() {{ echo "dvsi:x:1000:1000::{home}:/bin/bash"; }}
xdpyinfo() {{ return 0; }}
runuser() {{ shift 3; "$@"; }}
su() {{ return 1; }}
""")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"wayland=0 display=:0 xauth={home}/.Xauthority"


@pytest.mark.parametrize("script", SCRIPTS)
def test_x11_without_xdpyinfo_or_xset_takes_the_first_readable_xauthority(script, tmp_path):
    # x11-utils is not always installed: nothing can verify the display, which
    # must not end the install.
    home = tmp_path / "dvsi"
    home.mkdir()
    (home / ".Xauthority").write_text("cookie")
    bin_dir = tmp_path / "bin"   # the real tools, minus xdpyinfo and xset
    bin_dir.mkdir()
    for tool in ("grep", "head", "tr", "cut", "sed", "awk", "cat", "env"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    r = detect(script, rf"""
PATH={bin_dir}
pgrep() {{ return 1; }}
ls() {{ return 1; }}
ps() {{ printf '/usr/lib/xorg/Xorg :0 -seat seat0 -auth /var/run/lightdm/root/:0 vt7\n'; }}
getent() {{ echo "dvsi:x:1000:1000::{home}:/bin/bash"; }}
runuser() {{ shift 3; "$@"; }}
su() {{ return 1; }}
""")
    assert r.returncode == 0, f"install stopped in detect_display: {r.stdout!r} {r.stderr!r}"
    assert r.stdout.strip() == f"wayland=0 display=:0 xauth={home}/.Xauthority"


@pytest.mark.parametrize("script", SCRIPTS)
def test_no_display_server_found_keeps_the_default_and_carries_on(script, tmp_path):
    # Installed over SSH before the desktop is up: neither Wayland nor Xorg yet.
    r = detect(script, rf"""
pgrep() {{ return 1; }}
ls() {{ return 1; }}
ps() {{ printf '/usr/sbin/sshd -D\n'; }}
getent() {{ echo "dvsi:x:1000:1000::{tmp_path}:/bin/bash"; }}
xdpyinfo() {{ return 1; }}
runuser() {{ shift 3; "$@"; }}
su() {{ return 1; }}
""")
    assert r.returncode == 0, f"install stopped in detect_display: {r.stdout!r} {r.stderr!r}"
    assert r.stdout.strip() == "wayland=0 display=:0 xauth="


# --- snap Chromium and the service unit -----------------------------------------

def snap_check(script: str, stubs: str) -> str:
    r = run_bash(PRELUDE + stubs + function_source(script, "chromium_is_snap")
                 + "if chromium_is_snap; then echo snap; else echo not-snap; fi\n")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.parametrize("script", SCRIPTS)
def test_snap_chromium_is_detected(script):
    assert snap_check(script, 'snap() { [[ "$1 $2" == "list chromium" ]]; }\n') == "snap"


@pytest.mark.parametrize("script", SCRIPTS)
def test_chromium_not_installed_as_a_snap(script):
    assert snap_check(script, "snap() { return 1; }\n") == "not-snap"


@pytest.mark.parametrize("script", SCRIPTS)
def test_no_snapd_at_all(script):
    assert snap_check(script, "PATH=/nonexistent\n") == "not-snap"


@pytest.mark.parametrize("script", SCRIPTS)
def test_service_no_new_privileges_follows_the_snap_check(script):
    # snap-confine is setuid root: under NoNewPrivileges=true it refuses to start
    # and a snap Chromium kiosk exits with code 1 at every launch.
    unit = service_unit(script)
    assert "NoNewPrivileges=$NO_NEW_PRIVILEGES" in unit
    assert "NoNewPrivileges=true" not in unit


@pytest.mark.parametrize("script", SCRIPTS)
def test_service_starts_with_the_desktop_without_an_ordering_cycle(script):
    # WantedBy=multi-user.target + After=graphical.target is a cycle: systemd
    # drops the player's start job at every boot.
    unit = service_unit(script)
    assert "WantedBy=graphical.target" in unit
    assert not re.search(r"^After=.*graphical\.target", unit, re.M)


@pytest.mark.parametrize("script", SCRIPTS)
def test_service_shares_the_x_sockets_despite_private_tmp(script):
    unit = service_unit(script)
    assert "PrivateTmp=true" in unit
    assert "BindPaths=-/tmp/.X11-unix" in unit

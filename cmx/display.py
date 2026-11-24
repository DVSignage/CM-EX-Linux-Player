"""
Finding and keeping the desktop the player draws on.

Stock Ubuntu runs GNOME on Wayland. The player stays on X11 through XWayland
(Chromium is started with --ozone-platform=x11, mpv uses X11), which needs:
  - the X socket of the session (/tmp/.X11-unix/X<n>), which appears only
    once the user's session has started — the service may start earlier;
  - its auth file: on GNOME/Wayland `/run/user/<uid>/.mutter-Xwaylandauth.*`,
    a NEW random name at every login, so it cannot be baked into the systemd
    unit at install time; on KDE `/run/user/<uid>/xauth_*`; on X11 sessions
    GDM's `/run/user/<uid>/gdm/Xauthority` or `~/.Xauthority`;
  - the session bus, to keep GNOME from blanking, locking or showing banners.
"""

import asyncio
import glob
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("player")

WAIT_FOR_DISPLAY = 180.0


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def _newest(pattern: str) -> Optional[str]:
    files = [f for f in glob.glob(pattern) if os.path.isfile(f)]
    return max(files, key=os.path.getmtime) if files else None


def find_xauthority(uid: Optional[int] = None, home: Optional[Path] = None,
                    current: Optional[str] = None, run_dir: Optional[str] = None) -> Optional[str]:
    """The live X auth file for this user's session, newest first."""
    uid = _uid() if uid is None else uid
    run = run_dir or f"/run/user/{uid}"
    home = home or Path.home()
    candidates = [
        _newest(f"{run}/.mutter-Xwaylandauth.*"),   # GNOME on Wayland (XWayland)
        _newest(f"{run}/xauth_*"),                  # KDE / SDDM
        f"{run}/gdm/Xauthority",                    # GNOME on X11
        str(home / ".Xauthority"),
    ]
    found = [c for c in candidates if c and os.path.isfile(c)]
    if current and os.path.isfile(current):
        # a configured file is kept only while it is the live one (or the only one)
        if not found or current in found[:1]:
            return current
    return found[0] if found else None


def session_type(uid: Optional[int] = None) -> str:
    t = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if t in ("wayland", "x11"):
        return t
    uid = _uid() if uid is None else uid
    return "wayland" if glob.glob(f"/run/user/{uid}/wayland-*") else "x11"


def x_socket(display: str) -> str:
    num = display.split(":")[-1].split(".")[0] or "0"
    return f"/tmp/.X11-unix/X{num}"


def apply_environment(display: str) -> dict:
    """Point DISPLAY, XAUTHORITY, XDG_RUNTIME_DIR and the session bus at the
    live session. Returns what was found, for the log."""
    uid = _uid()
    os.environ.setdefault("DISPLAY", display)
    run = f"/run/user/{uid}"
    if os.path.isdir(run):
        os.environ.setdefault("XDG_RUNTIME_DIR", run)
        if os.path.exists(f"{run}/bus"):
            os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={run}/bus")
    auth = find_xauthority(uid, current=os.environ.get("XAUTHORITY"))
    if auth:
        os.environ["XAUTHORITY"] = auth
    return {"display": os.environ.get("DISPLAY"), "xauthority": auth, "session": session_type(uid),
            "socket": os.path.exists(x_socket(os.environ["DISPLAY"]))}


async def wait_for_display(display: str, timeout: float = WAIT_FOR_DISPLAY) -> dict:
    """Wait (bounded) for the user's session: the X socket, and on a Wayland
    session its XWayland auth file. A service that starts before the desktop
    then comes up cleanly instead of failing and restarting in a loop."""
    started = time.monotonic()
    logged = False
    while True:
        info = apply_environment(display)
        ready = info["socket"] and (info["xauthority"] or info["session"] == "x11")
        if ready or time.monotonic() - started > timeout:
            if not ready:
                log.warning(f"[DISPLAY] no desktop session after {timeout:.0f}s ({info}) — starting anyway")
            return info
        if not logged:
            log.info("[DISPLAY] waiting for the desktop session (X socket / XWayland auth)…")
            logged = True
        await asyncio.sleep(2)


GNOME_KEYS = [
    ("org.gnome.desktop.session", "idle-delay", "uint32 0"),       # never blank
    ("org.gnome.desktop.screensaver", "lock-enabled", "false"),    # never lock
    ("org.gnome.desktop.screensaver", "idle-activation-enabled", "false"),
    ("org.gnome.desktop.notifications", "show-banners", "false"),  # no pop-ups over content
    ("org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type", "'nothing'"),
]


def keep_desktop_awake() -> int:
    """GNOME would blank, lock and show notification banners over the
    content: turn those off for this user (best effort; returns keys set)."""
    if not shutil.which("gsettings") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return 0
    done = 0
    for schema, key, value in GNOME_KEYS:
        try:
            r = subprocess.run(["gsettings", "set", schema, key, value], capture_output=True, timeout=5)
            done += r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    if done:
        log.info(f"[DISPLAY] desktop kept awake ({done} GNOME settings: no blanking, lock or banners)")
    return done

"""
Machine inventory — the Linux counterpart of DVSI Plus `Cmx/MachineInventory.cs`
and `InventoryReporter.cs`.

What is reported (an `inventory` telemetry event): the OS, whether a reboot
is pending, whether the player runs as root, the LAN address, the remote
access tools present (SSH, xrdp, VNC, AnyDesk, TeamViewer, RustDesk) and the
installed applications (dpkg or rpm packages, libraries left out).

When: the first report a minute after the routines start; then a change
check every 5 minutes; a full report every 24 h or after a change, but never
within 15 minutes of the last one. Nothing without a player secret.
"""

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

try:
    import psutil as _psutil
except ImportError:
    _psutil = None

log = logging.getLogger("player")

AGENT_NAME = "cmex_linux_player"
MAX_APPS = 500
MAX_REMOTE = 16
MAX_PORTS = 8
FIRST_DELAY = 60.0
CHECK_EVERY = 300.0
FULL_EVERY = 86400.0
MIN_GAP = 900.0
LIB_SECTIONS = {"libs", "oldlibs", "libdevel", "debug", "kernel", "localization", "fonts", "doc"}

REMOTE_TOOLS = [
    # (tool, display name, process names, systemd units, default ports)
    ("ssh", "OpenSSH server", ["sshd"], ["ssh", "sshd"], [22]),
    ("rdp", "xrdp", ["xrdp"], ["xrdp"], [3389]),
    ("vnc", "VNC server", ["x11vnc", "Xvnc", "vino-server", "wayvnc", "Xtigervnc"], ["x11vnc", "vncserver"], [5900]),
    ("anydesk", "AnyDesk", ["anydesk"], ["anydesk"], [7070]),
    ("teamviewer", "TeamViewer", ["teamviewerd"], ["teamviewerd"], [5938]),
    ("rustdesk", "RustDesk", ["rustdesk"], ["rustdesk"], [21118]),
]


def _run(args: List[str], timeout: float = 20) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def os_release() -> Dict[str, str]:
    out = {}
    for p in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            for line in Path(p).read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"')
            break
        except OSError:
            continue
    return out


def installed_apps() -> List[dict]:
    apps: List[dict] = []
    if shutil.which("dpkg-query"):
        text = _run(["dpkg-query", "-W", "-f", "${Package}\t${Version}\t${Maintainer}\t${Section}\t${db:Status-Abbrev}\n"])
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) < 5 or not parts[4].startswith("ii"):
                continue
            name, version, maint, section = parts[:4]
            if section.split("/")[-1] in LIB_SECTIONS or name.startswith("lib"):
                continue
            apps.append({"name": name, "version": version, "publisher": re.sub(r"\s*<.*?>", "", maint) or None,
                         "installed": None})
    elif shutil.which("rpm"):
        text = _run(["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{VENDOR}\t%{INSTALLTIME}\n"])
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) < 4 or parts[0].startswith("lib"):
                continue
            try:
                when = datetime.fromtimestamp(int(parts[3]), timezone.utc).strftime("%Y-%m-%d")
            except ValueError:
                when = None
            apps.append({"name": parts[0], "version": parts[1],
                         "publisher": None if parts[2] in ("", "(none)") else parts[2], "installed": when})
    apps.sort(key=lambda a: (a["name"].lower(), a["version"] or ""))
    return apps


def apps_hash(apps: List[dict]) -> str:
    lines = "\n".join(f"{a['name']}\t{a.get('version') or ''}\t{a.get('publisher') or ''}\t{a.get('installed') or ''}"
                      for a in apps)
    return "sha256:" + hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _ssh_details() -> dict:
    ports, auth = [], None
    try:
        for line in Path("/etc/ssh/sshd_config").read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.lower().startswith("port "):
                try:
                    ports.append(int(line.split()[1]))
                except (IndexError, ValueError):
                    pass
            if line.lower().startswith("passwordauthentication "):
                auth = "password" if line.split()[1].lower() == "yes" else "key"
    except OSError:
        pass
    return {"ports": ports or [22], "auth": auth or "password"}


def remote_access() -> List[dict]:
    running = set()
    if _psutil is not None:
        try:
            running = {p.info["name"] for p in _psutil.process_iter(["name"]) if p.info.get("name")}
        except Exception:
            pass
    out = []
    for tool, name, procs, units, ports in REMOTE_TOOLS:
        active = any(p in running for p in procs)
        unit = None
        if not active and shutil.which("systemctl"):
            for u in units:
                if _run(["systemctl", "is-active", u], timeout=5).strip() == "active":
                    active, unit = True, u
                    break
        installed = active or any(shutil.which(p) for p in procs)
        if not installed:
            continue
        entry = {"tool": tool, "name": name, "version": None, "service": unit or (units[0] if active else None),
                 "ports": ports[:MAX_PORTS], "auth": None, "nla": None}
        if tool == "ssh":
            entry.update(_ssh_details())
            entry["ports"] = entry["ports"][:MAX_PORTS]
        out.append(entry)
    return out[:MAX_REMOTE]


def collect(version: str, lan_ip: Optional[str]) -> dict:
    """The inventory body, without player_uuid/ts (added when it is sent)."""
    rel = os_release()
    apps = installed_apps()
    return {
        "type": "inventory",
        "kind": "screen",
        "agent": {"name": AGENT_NAME, "version": version},
        "os": {"name": rel.get("NAME") or platform.system(), "version": rel.get("VERSION_ID") or "",
               "arch": platform.machine(), "release": platform.release()},
        "system": {"pending_reboot": Path("/var/run/reboot-required").exists(),
                   "runs_as_admin": hasattr(os, "geteuid") and os.geteuid() == 0,
                   "webview2": None, "dotnet": None, "lan_ip": lan_ip},
        "remote_access": remote_access(),
        "apps": apps[:MAX_APPS],
        "apps_count": len(apps),
        "apps_truncated": len(apps) > MAX_APPS,
        "apps_hash": apps_hash(apps),
    }


def fingerprint(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


class InventoryReporter:
    """Decides when an inventory is due; persists the last one sent."""

    def __init__(self, state_path: Path, clock=time.time):
        self.state_path = state_path
        self._clock = clock
        try:
            self.state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}

    def due(self, body: dict) -> bool:
        now = self._clock()
        last = float(self.state.get("last_sent", 0))
        if now - last < MIN_GAP:
            return False
        changed = fingerprint(body) != self.state.get("fingerprint")
        return changed or now - last >= FULL_EVERY

    def sent(self, body: dict) -> None:
        self.state = {"last_sent": self._clock(), "fingerprint": fingerprint(body)}
        try:
            self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        except OSError:
            pass

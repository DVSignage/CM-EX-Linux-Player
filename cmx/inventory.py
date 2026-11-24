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
    # (tool id, display name, process names, systemd units, default ports, packages)
    # Ids follow CM:EX's list where one exists (rdp, realvnc, teamviewer, anydesk,
    # rustdesk); a tool outside it keeps its own id.
    ("ssh", "OpenSSH server", ["sshd"], ["ssh", "sshd"], [22], ["openssh-server"]),
    ("rdp", "xrdp", ["xrdp"], ["xrdp"], [3389], ["xrdp"]),
    ("realvnc", "RealVNC Server", ["vncserver-x11-core", "vncserver-x11-serviced", "vncserver-virtuald"],
     ["vncserver-x11-serviced"], [5900], ["realvnc-vnc-server"]),
    ("wayvnc", "wayvnc", ["wayvnc"], ["wayvnc"], [5900], ["wayvnc"]),
    ("x11vnc", "x11vnc", ["x11vnc"], ["x11vnc"], [5900], ["x11vnc"]),
    ("tigervnc", "TigerVNC", ["Xtigervnc", "Xvnc"], [], [5901], ["tigervnc-standalone-server", "tigervnc-server"]),
    ("vino", "Vino", ["vino-server"], [], [5900], ["vino"]),
    ("anydesk", "AnyDesk", ["anydesk"], ["anydesk"], [7070], ["anydesk"]),
    ("teamviewer", "TeamViewer", ["teamviewerd"], ["teamviewerd"], [5938], ["teamviewer"]),
    ("rustdesk", "RustDesk", ["rustdesk"], ["rustdesk"], [21118], ["rustdesk"]),
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


def _ssh_details(etc: Path = Path("/etc/ssh")) -> dict:
    """Ports and whether SSH asks for a password. sshd keeps the FIRST value it
    reads, and sshd_config includes sshd_config.d/*.conf at its top, so the
    drop-ins win. Key-only logins are not "none" (open): they are reported as
    unknown, the CMS vocabulary being password / none / unknown."""
    ports, auth = [], None
    files = sorted((etc / "sshd_config.d").glob("*.conf")) + [etc / "sshd_config"]
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.strip().split()
            if len(parts) < 2 or parts[0].startswith("#"):
                continue
            key = parts[0].lower()
            if key == "port":
                try:
                    ports.append(int(parts[1]))
                except ValueError:
                    pass
            elif key == "passwordauthentication" and auth is None:
                auth = "password" if parts[1].lower() == "yes" else "unknown"
    return {"ports": ports or [22], "auth": auth or "password"}


def package_version(packages: List[str]) -> Optional[str]:
    for pkg in packages:
        if shutil.which("dpkg-query"):
            v = _run(["dpkg-query", "-W", "-f", "${Version}", pkg], timeout=5).strip()
        elif shutil.which("rpm"):
            out = _run(["rpm", "-q", "--qf", "%{VERSION}", pkg], timeout=5).strip()
            v = "" if "not installed" in out else out
        else:
            v = ""
        if v:
            return v
    return None


def service_state(units: List[str], running: bool) -> str:
    """running / stopped / absent, the CMS's words for a tool's service."""
    if running:
        return "running"
    if shutil.which("systemctl"):
        for u in units:
            state = _run(["systemctl", "is-active", u], timeout=5).strip()
            if state == "active":
                return "running"
            if state in ("inactive", "failed", "activating", "deactivating"):
                return "stopped"
    return "absent"


def remote_access() -> List[dict]:
    running = set()
    if _psutil is not None:
        try:
            running = {p.info["name"] for p in _psutil.process_iter(["name"]) if p.info.get("name")}
        except Exception:
            pass
    out = []
    for tool, name, procs, units, ports, packages in REMOTE_TOOLS:
        state = service_state(units, any(p in running for p in procs))
        version = package_version(packages)
        if state == "absent" and version is None and not any(shutil.which(p) for p in procs):
            continue
        entry = {"tool": tool, "name": name, "version": version, "service": state,
                 "ports": ports[:MAX_PORTS], "auth": "unknown", "nla": None}
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
        version = (body.get("agent") or {}).get("version")
        if self.state.get("refused_version") and self.state.get("refused_version") == version:
            return now - last >= FULL_EVERY      # the cloud refused this agent's report: daily only
        changed = fingerprint(body) != self.state.get("fingerprint")
        return changed or now - last >= FULL_EVERY

    def refused(self, version: Optional[str]) -> None:
        """The cloud refused an inventory: report daily only, until the agent
        version changes (a newer player may send what the cloud accepts)."""
        self.state["refused_version"] = version or "?"
        self._save()

    def sent(self, body: dict) -> None:
        refused = self.state.get("refused_version")
        self.state = {"last_sent": self._clock(), "fingerprint": fingerprint(body)}
        if refused and refused == (body.get("agent") or {}).get("version"):
            self.state["refused_version"] = refused
        self._save()

    def _save(self) -> None:
        try:
            self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        except OSError:
            pass

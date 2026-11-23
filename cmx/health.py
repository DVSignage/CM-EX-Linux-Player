"""
System health for the heartbeat: metrics, the LAN address, NDI availability.

The LAN address follows DVSI Plus `Cmx/LanAddress.cs`: the local end of a
UDP socket "connected" towards the CMS host (no packet is sent), then the
same towards 8.8.8.8, then any up, non-virtual adapter; loopback, 0.0.0.0,
link-local and multicast addresses are never reported.
"""

import ipaddress
import logging
import re
import socket
from typing import Callable, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

try:
    import psutil as _psutil
except ImportError:   # optional
    _psutil = None

log = logging.getLogger("player")

OUTSIDE_PROBE = "8.8.8.8"

# Host sides of virtual switches / container bridges on Linux.
_VIRTUAL_NIC = re.compile(r"^(lo|docker\d*|br-|veth|virbr|vmnet|vboxnet|cni|flannel|tun|tap|zt|wg)", re.IGNORECASE)


def is_usable(ip: Optional[str]) -> bool:
    if not ip:
        return False
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_loopback or a.is_unspecified or a.is_link_local or a.is_multicast:
        return False
    if a.version == 4 and int(str(a).split(".")[0]) >= 224:
        return False
    return True


def source_towards(host: str) -> Optional[str]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(2)
        s.connect((host, 65530))
        return s.getsockname()[0]
    finally:
        s.close()


def adapters() -> List[Tuple[str, bool]]:
    """(address, is_virtual) for IPv4 addresses of adapters that are up."""
    found: List[Tuple[str, bool]] = []
    if _psutil is None:
        return found
    try:
        stats = _psutil.net_if_stats()
        for name, addrs in _psutil.net_if_addrs().items():
            if name in stats and not stats[name].isup:
                continue
            for a in addrs:
                if a.family == socket.AF_INET:
                    found.append((a.address, bool(_VIRTUAL_NIC.match(name))))
    except Exception:
        pass
    return found


def pick_lan_address(server_host: Optional[str],
                     towards: Callable[[str], Optional[str]] = source_towards,
                     nics: Callable[[], Iterable[Tuple[str, bool]]] = adapters) -> Optional[str]:
    def attempt(fn, *a):
        try:
            return fn(*a)
        except Exception:
            return None

    first = (server_host or "").strip() or OUTSIDE_PROBE
    ip = attempt(towards, first)
    if is_usable(ip):
        return ip
    if first != OUTSIDE_PROBE:
        ip = attempt(towards, OUTSIDE_PROBE)
        if is_usable(ip):
            return ip
    for addr, virtual in attempt(lambda: list(nics())) or []:
        if not virtual and is_usable(addr):
            return addr
    return None


def lan_address_for(cms_url: str) -> Optional[str]:
    try:
        host = urlsplit(cms_url).hostname
    except ValueError:
        host = None
    return pick_lan_address(host)


def metrics() -> dict:
    """cpu/memory/disk percent and CPU temperature; an unreadable value is
    null (unknown), never zero."""
    out = {"cpu_percent": None, "memory_percent": None, "disk_percent": None, "cpu_temp": None}
    if _psutil is None:
        return out
    try:
        out["cpu_percent"] = round(_psutil.cpu_percent(interval=None), 1)
        out["memory_percent"] = round(_psutil.virtual_memory().percent, 1)
        out["disk_percent"] = round(_psutil.disk_usage("/").percent, 1)
    except Exception:
        pass
    try:
        temps = _psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "cpu-thermal"):
            if temps.get(key):
                out["cpu_temp"] = round(temps[key][0].current, 1)
                break
    except Exception:
        pass
    return out

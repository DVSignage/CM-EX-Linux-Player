"""
Configuration (config.yaml), on-disk locations and the persisted player state.

state.json holds the player's identity and its secret (`player_secret`, shown
by the CMS exactly once at approval), so it is written owner-only (0600).
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("player")

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "signage-player"

CONFIG_SEARCH = [
    Path("config.yaml"),
    Path("/etc/signage-player/config.yaml"),
    DEFAULT_CONFIG_DIR / "config.yaml",
]


@dataclass(frozen=True)
class Paths:
    """Every file the player keeps, rooted at one directory (tests use a temp dir)."""

    base: Path

    @property
    def state(self) -> Path:
        return self.base / "state.json"

    @property
    def offline_playlist(self) -> Path:
        return self.base / "offline_playlist.json"

    @property
    def cache_dir(self) -> Path:
        return self.base / "cache"

    @property
    def cache_history(self) -> Path:
        return self.base / "cache-history.json"

    @property
    def telemetry_queue(self) -> Path:
        return self.base / "telemetry-queue.json"

    @property
    def crash_dir(self) -> Path:
        return self.base / "crash"

    @property
    def screens_dir(self) -> Path:
        return self.base / "screens"

    @property
    def local_api_key(self) -> Path:
        return self.base / "local-api.key"

    @property
    def preview_tmp(self) -> Path:
        return self.base / "preview.jpg"

    @property
    def browser_profile(self) -> Path:
        return self.base / "chromium-profile"

    def ensure(self) -> None:
        for d in (self.base, self.cache_dir, self.crash_dir, self.screens_dir):
            d.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    """The parts of config.yaml the player reads, with their defaults."""

    cms_url: str = ""
    display: str = ":0"
    audio_output: str = "auto"
    default_image_duration: int = 10
    log_level: str = "INFO"
    keep_playlists: int = 10

    @classmethod
    def from_dict(cls, cfg: dict) -> "Settings":
        pb = cfg.get("playback") or {}
        cache = cfg.get("cache") or {}
        try:
            img = int(pb.get("default_image_duration", 10))
        except (TypeError, ValueError):
            img = 10
        try:
            keep = int(cache.get("keep_playlists", 10))
        except (TypeError, ValueError):
            keep = 10
        return cls(
            cms_url=str(cfg.get("cms_url") or "").strip().rstrip("/"),
            display=str(pb.get("display") or ":0"),
            audio_output=str(pb.get("audio_output") or "auto"),
            default_image_duration=img if img > 0 else 10,
            log_level=str(cfg.get("log_level") or "INFO").upper(),
            keep_playlists=min(100, max(1, keep)),
        )


def load_config(search: Optional[list] = None) -> dict:
    """Load config.yaml from the first location that has one."""
    for p in search or CONFIG_SEARCH:
        if p.exists():
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            log.info(f"Loaded config from {p}")
            return data
    log.warning("No config.yaml found — using defaults.")
    return {}


def load_state(paths: Paths) -> dict:
    paths.ensure()
    if paths.state.exists():
        try:
            return json.loads(paths.state.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"Failed to load state: {e}")
    return {}


def save_state(paths: Paths, state: dict) -> None:
    """Write state.json atomically and owner-only: it carries the player secret."""
    paths.base.mkdir(parents=True, exist_ok=True)
    tmp = paths.state.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, paths.state)


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)

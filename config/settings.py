"""
Configuration management for the player daemon.
"""

import os
import uuid
from pathlib import Path
from typing import Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings


class PlayerConfig(BaseSettings):
    """Player configuration."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = Field(default="Digital Signage Player")


class APIConfig(BaseSettings):
    """API configuration."""

    base_url: str = Field(default="http://localhost:8000/api/v1")
    heartbeat_interval: int = Field(default=15)
    timeout: int = Field(default=10)


class NetworkConfig(BaseSettings):
    """Network share configuration."""

    share_path: Optional[str] = Field(default=None)
    username: Optional[str] = Field(default=None)
    password: Optional[str] = Field(default=None)
    scan_interval: int = Field(default=300)


class CacheConfig(BaseSettings):
    """Cache configuration."""

    directory: Path = Field(default=Path("/var/lib/signage-player/cache"))
    max_size_gb: int = Field(default=50)
    eviction_policy: str = Field(default="lru")
    prefetch_count: int = Field(default=3)


class PlaybackConfig(BaseSettings):
    """Playback configuration."""

    default_image_duration: int = Field(default=10)
    transition_type: str = Field(default="cut")
    audio_output: str = Field(default="auto")
    display: str = Field(default=":0")


class Settings(BaseSettings):
    """Application settings."""

    player: PlayerConfig = Field(default_factory=PlayerConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    playback: PlaybackConfig = Field(default_factory=PlaybackConfig)

    log_level: str = Field(default="INFO")

    class Config:
        env_file = ".env"
        env_nested_delimiter = "__"


def load_settings(config_path: Optional[Path] = None) -> Settings:
    """
    Load settings from YAML config file.

    Args:
        config_path: Path to config file. If None, searches for config.yaml
                    in current directory and /etc/signage-player/

    Returns:
        Settings object
    """
    if config_path is None:
        # Search for config file
        search_paths = [
            Path("config.yaml"),
            Path("/etc/signage-player/config.yaml"),
            Path.home() / ".config" / "signage-player" / "config.yaml",
        ]
        for path in search_paths:
            if path.exists():
                config_path = path
                break

    if config_path and config_path.exists():
        with open(config_path, "r") as f:
            config_data = yaml.safe_load(f)

        # Convert nested dict to Settings
        settings = Settings(**config_data)
    else:
        # Use defaults
        settings = Settings()

    # Ensure cache directory exists
    settings.cache.directory.mkdir(parents=True, exist_ok=True)

    return settings


def generate_default_config() -> str:
    """
    Generate a default configuration YAML string.

    Returns:
        YAML configuration string
    """
    config = {
        "player": {
            "id": "uuid-or-hostname",
            "name": "Lobby Display"
        },
        "api": {
            "base_url": "http://cms-server:8000/api/v1",
            "heartbeat_interval": 15,
            "timeout": 10
        },
        "network": {
            "share_path": "smb://server/signage-content",
            "username": "player",
            "password": "secure-password",
            "scan_interval": 300
        },
        "cache": {
            "directory": "/var/lib/signage-player/cache",
            "max_size_gb": 50,
            "eviction_policy": "lru",
            "prefetch_count": 3
        },
        "playback": {
            "default_image_duration": 10,
            "transition_type": "cut",
            "audio_output": "auto",
            "display": ":0"
        },
        "log_level": "INFO"
    }

    return yaml.dump(config, default_flow_style=False, sort_keys=False)

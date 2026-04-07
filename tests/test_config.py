"""
Tests for configuration management.
"""

import pytest
import tempfile
from pathlib import Path

from config.settings import Settings, load_settings, generate_default_config


def test_default_settings():
    """Test default settings creation."""
    settings = Settings()

    assert settings.player.name == "Digital Signage Player"
    assert settings.api.base_url == "http://localhost:8000/api/v1"
    assert settings.api.heartbeat_interval == 15
    assert settings.cache.max_size_gb == 50
    assert settings.playback.default_image_duration == 10


def test_generate_default_config():
    """Test generating default config YAML."""
    config_yaml = generate_default_config()

    assert "player:" in config_yaml
    assert "api:" in config_yaml
    assert "cache:" in config_yaml
    assert "playback:" in config_yaml


def test_load_settings_no_file():
    """Test loading settings when no config file exists."""
    settings = load_settings(Path("/nonexistent/config.yaml"))

    # Should use defaults
    assert settings.player.name == "Digital Signage Player"


def test_load_settings_from_yaml():
    """Test loading settings from YAML file."""
    config_content = """
player:
  id: "test-player-123"
  name: "Test Display"

api:
  base_url: "http://test-server:8000/api/v1"
  heartbeat_interval: 30

playback:
  default_image_duration: 15
"""

    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        temp_path = Path(f.name)

    try:
        settings = load_settings(temp_path)

        assert settings.player.id == "test-player-123"
        assert settings.player.name == "Test Display"
        assert settings.api.base_url == "http://test-server:8000/api/v1"
        assert settings.api.heartbeat_interval == 30
        assert settings.playback.default_image_duration == 15

    finally:
        temp_path.unlink()

"""
Tests for cache manager.
"""

import pytest
import tempfile
from pathlib import Path

from cache.manager import CacheManager


@pytest.fixture
def temp_cache_dir():
    """Create a temporary cache directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def cache_manager(temp_cache_dir):
    """Create a cache manager instance."""
    # 10 MB cache
    return CacheManager(temp_cache_dir, max_size_bytes=10 * 1024 * 1024)


@pytest.fixture
def test_file():
    """Create a temporary test file."""
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
        f.write("Test content for caching")
        temp_path = Path(f.name)

    yield temp_path

    # Cleanup
    if temp_path.exists():
        temp_path.unlink()


def test_cache_add(cache_manager, test_file):
    """Test adding a file to cache."""
    cached_path = cache_manager.add("test-content-1", test_file)

    assert cached_path is not None
    assert cached_path.exists()
    assert cache_manager.has("test-content-1")


def test_cache_get(cache_manager, test_file):
    """Test retrieving a cached item."""
    cache_manager.add("test-content-1", test_file)

    entry = cache_manager.get("test-content-1")

    assert entry is not None
    assert entry.content_id == "test-content-1"
    assert entry.file_path.exists()


def test_cache_remove(cache_manager, test_file):
    """Test removing a cached item."""
    cache_manager.add("test-content-1", test_file)
    assert cache_manager.has("test-content-1")

    removed = cache_manager.remove("test-content-1")

    assert removed is True
    assert not cache_manager.has("test-content-1")


def test_cache_has(cache_manager, test_file):
    """Test checking if content is cached."""
    assert not cache_manager.has("test-content-1")

    cache_manager.add("test-content-1", test_file)

    assert cache_manager.has("test-content-1")


def test_cache_stats(cache_manager, test_file):
    """Test cache statistics."""
    stats = cache_manager.get_stats()
    assert stats["total_items"] == 0

    cache_manager.add("test-content-1", test_file)

    stats = cache_manager.get_stats()
    assert stats["total_items"] == 1
    assert stats["total_size_bytes"] > 0


def test_cache_clear(cache_manager, test_file):
    """Test clearing the cache."""
    cache_manager.add("test-content-1", test_file)
    cache_manager.add("test-content-2", test_file)

    assert cache_manager.get_entry_count() == 2

    cache_manager.clear()

    assert cache_manager.get_entry_count() == 0


def test_cache_duplicate_add(cache_manager, test_file):
    """Test adding the same content twice."""
    cached_path1 = cache_manager.add("test-content-1", test_file)
    cached_path2 = cache_manager.add("test-content-1", test_file)

    # Should return same path and not duplicate
    assert cached_path1 == cached_path2
    assert cache_manager.get_entry_count() == 1

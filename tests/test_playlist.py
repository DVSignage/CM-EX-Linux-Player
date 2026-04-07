"""
Tests for playlist module.
"""

import pytest
from pathlib import Path

from core.playlist import Playlist, PlaylistItem


def test_playlist_creation():
    """Test creating a playlist."""
    playlist = Playlist(
        id="test-playlist",
        name="Test Playlist",
        description="A test playlist"
    )

    assert playlist.id == "test-playlist"
    assert playlist.name == "Test Playlist"
    assert len(playlist) == 0
    assert playlist.loop is True


def test_playlist_add_item():
    """Test adding items to playlist."""
    playlist = Playlist(id="test", name="Test")

    item1 = PlaylistItem(
        id="item1",
        content_id="content1",
        file_path=Path("/test/video1.mp4"),
        duration=30,
        order=1
    )

    item2 = PlaylistItem(
        id="item2",
        content_id="content2",
        file_path=Path("/test/video2.mp4"),
        duration=45,
        order=0
    )

    playlist.add_item(item1)
    playlist.add_item(item2)

    assert len(playlist) == 2
    # Should be sorted by order
    assert playlist[0].id == "item2"
    assert playlist[1].id == "item1"


def test_playlist_get_next_item():
    """Test getting next item in playlist."""
    playlist = Playlist(id="test", name="Test", loop=True)

    for i in range(3):
        playlist.add_item(PlaylistItem(
            id=f"item{i}",
            content_id=f"content{i}",
            file_path=Path(f"/test/video{i}.mp4"),
            duration=10,
            order=i
        ))

    # Next from index 0 should be index 1
    next_item = playlist.get_next_item(0)
    assert next_item.id == "item1"

    # Next from last index should loop to first
    next_item = playlist.get_next_item(2)
    assert next_item.id == "item0"


def test_playlist_get_next_item_no_loop():
    """Test getting next item when loop is disabled."""
    playlist = Playlist(id="test", name="Test", loop=False)

    for i in range(3):
        playlist.add_item(PlaylistItem(
            id=f"item{i}",
            content_id=f"content{i}",
            file_path=Path(f"/test/video{i}.mp4"),
            duration=10,
            order=i
        ))

    # Next from last index should be None
    next_item = playlist.get_next_item(2)
    assert next_item is None


def test_playlist_get_previous_item():
    """Test getting previous item in playlist."""
    playlist = Playlist(id="test", name="Test", loop=True)

    for i in range(3):
        playlist.add_item(PlaylistItem(
            id=f"item{i}",
            content_id=f"content{i}",
            file_path=Path(f"/test/video{i}.mp4"),
            duration=10,
            order=i
        ))

    # Previous from index 1 should be index 0
    prev_item = playlist.get_previous_item(1)
    assert prev_item.id == "item0"

    # Previous from first index should loop to last
    prev_item = playlist.get_previous_item(0)
    assert prev_item.id == "item2"


def test_playlist_item_is_image():
    """Test checking if item is an image."""
    item = PlaylistItem(
        id="item1",
        content_id="content1",
        file_path=Path("/test/image.jpg"),
        duration=10,
        content_type="image"
    )

    assert item.is_image is True
    assert item.is_video is False


def test_playlist_item_effective_duration():
    """Test effective duration calculation."""
    # Item with explicit duration
    item1 = PlaylistItem(
        id="item1",
        content_id="content1",
        file_path=Path("/test/video.mp4"),
        duration=30
    )
    assert item1.effective_duration == 30.0

    # Item with auto duration (0)
    item2 = PlaylistItem(
        id="item2",
        content_id="content2",
        file_path=Path("/test/video.mp4"),
        duration=0
    )
    assert item2.effective_duration is None


def test_playlist_remove_item():
    """Test removing items from playlist."""
    playlist = Playlist(id="test", name="Test")

    item = PlaylistItem(
        id="item1",
        content_id="content1",
        file_path=Path("/test/video.mp4"),
        duration=10
    )

    playlist.add_item(item)
    assert len(playlist) == 1

    removed = playlist.remove_item("item1")
    assert removed is True
    assert len(playlist) == 0

    # Try to remove non-existent item
    removed = playlist.remove_item("nonexistent")
    assert removed is False

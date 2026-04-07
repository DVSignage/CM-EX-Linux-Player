"""
Playlist management module.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class PlaylistItem:
    """Represents a single item in a playlist."""

    id: str
    content_id: str
    file_path: Path
    duration: int  # seconds, 0 = auto (for videos)
    transition: str = "cut"
    order: int = 0
    content_type: str = "video"  # video, image, audio

    @property
    def is_image(self) -> bool:
        """Check if this item is an image."""
        return self.content_type == "image"

    @property
    def is_video(self) -> bool:
        """Check if this item is a video."""
        return self.content_type == "video"

    @property
    def effective_duration(self) -> Optional[float]:
        """
        Get the effective duration for this item.

        Returns:
            Duration in seconds, or None for auto (video duration)
        """
        if self.duration > 0:
            return float(self.duration)
        return None  # Let mpv determine duration for videos


@dataclass
class Playlist:
    """Represents a playlist."""

    id: str
    name: str
    description: str = ""
    items: List[PlaylistItem] = None
    loop: bool = True

    def __post_init__(self):
        """Initialize items list if not provided."""
        if self.items is None:
            self.items = []

    def add_item(self, item: PlaylistItem) -> None:
        """
        Add an item to the playlist.

        Args:
            item: PlaylistItem to add
        """
        self.items.append(item)
        self._sort_items()

    def remove_item(self, item_id: str) -> bool:
        """
        Remove an item from the playlist.

        Args:
            item_id: ID of the item to remove

        Returns:
            True if item was removed, False if not found
        """
        for i, item in enumerate(self.items):
            if item.id == item_id:
                self.items.pop(i)
                return True
        return False

    def get_item(self, item_id: str) -> Optional[PlaylistItem]:
        """
        Get an item by ID.

        Args:
            item_id: ID of the item

        Returns:
            PlaylistItem if found, None otherwise
        """
        for item in self.items:
            if item.id == item_id:
                return item
        return None

    def get_next_item(self, current_index: int) -> Optional[PlaylistItem]:
        """
        Get the next item in the playlist.

        Args:
            current_index: Current item index

        Returns:
            Next PlaylistItem, or None if at end (and not looping)
        """
        if not self.items:
            return None

        next_index = current_index + 1

        if next_index >= len(self.items):
            if self.loop:
                next_index = 0
            else:
                return None

        return self.items[next_index]

    def get_previous_item(self, current_index: int) -> Optional[PlaylistItem]:
        """
        Get the previous item in the playlist.

        Args:
            current_index: Current item index

        Returns:
            Previous PlaylistItem, or None if at beginning (and not looping)
        """
        if not self.items:
            return None

        prev_index = current_index - 1

        if prev_index < 0:
            if self.loop:
                prev_index = len(self.items) - 1
            else:
                return None

        return self.items[prev_index]

    def _sort_items(self) -> None:
        """Sort items by order field."""
        self.items.sort(key=lambda x: x.order)

    def __len__(self) -> int:
        """Get the number of items in the playlist."""
        return len(self.items)

    def __getitem__(self, index: int) -> PlaylistItem:
        """Get an item by index."""
        return self.items[index]

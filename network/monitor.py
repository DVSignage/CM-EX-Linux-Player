"""
Network share monitoring and file discovery.
"""

import asyncio
import logging
from pathlib import Path
from typing import Callable, List, Optional, Set
from urllib.parse import urlparse

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

from ..utils.media import is_supported_media

logger = logging.getLogger(__name__)


class ShareMonitorEventHandler(FileSystemEventHandler):
    """Handler for file system events on network share."""

    def __init__(self, on_file_added: Callable, on_file_modified: Callable, on_file_deleted: Callable):
        """
        Initialize the event handler.

        Args:
            on_file_added: Callback for file added events
            on_file_modified: Callback for file modified events
            on_file_deleted: Callback for file deleted events
        """
        super().__init__()
        self.on_file_added = on_file_added
        self.on_file_modified = on_file_modified
        self.on_file_deleted = on_file_deleted

    def on_created(self, event: FileSystemEvent) -> None:
        """Handle file created event."""
        if not event.is_directory:
            file_path = Path(event.src_path)
            if is_supported_media(file_path):
                logger.info(f"File added: {file_path}")
                self.on_file_added(file_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        """Handle file modified event."""
        if not event.is_directory:
            file_path = Path(event.src_path)
            if is_supported_media(file_path):
                logger.info(f"File modified: {file_path}")
                self.on_file_modified(file_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        """Handle file deleted event."""
        if not event.is_directory:
            file_path = Path(event.src_path)
            logger.info(f"File deleted: {file_path}")
            self.on_file_deleted(file_path)


class NetworkShareMonitor:
    """
    Monitors a network share for content changes.
    """

    def __init__(self, share_path: str):
        """
        Initialize the network share monitor.

        Args:
            share_path: Path to network share (local path, SMB, or NFS)
        """
        self.share_path = share_path
        self.local_mount_point: Optional[Path] = None
        self.observer: Optional[Observer] = None
        self._running = False

        # Parse share path
        self._parse_share_path()

    def _parse_share_path(self) -> None:
        """Parse the share path to determine type and mount point."""
        parsed = urlparse(self.share_path)

        if parsed.scheme in ["smb", "cifs"]:
            # SMB share - would need to be mounted
            # For simplicity, assume it's already mounted at a local path
            # In production, implement proper SMB mounting
            logger.warning("SMB shares must be pre-mounted. Using path as-is.")
            self.local_mount_point = Path(self.share_path.replace("smb://", "/mnt/"))

        elif parsed.scheme == "nfs":
            # NFS share - would need to be mounted
            logger.warning("NFS shares must be pre-mounted. Using path as-is.")
            self.local_mount_point = Path(self.share_path.replace("nfs://", "/mnt/"))

        else:
            # Assume local path
            self.local_mount_point = Path(self.share_path)

        logger.info(f"Monitoring path: {self.local_mount_point}")

    def start(
        self,
        on_file_added: Callable,
        on_file_modified: Callable,
        on_file_deleted: Callable
    ) -> bool:
        """
        Start monitoring the network share.

        Args:
            on_file_added: Callback for file added events
            on_file_modified: Callback for file modified events
            on_file_deleted: Callback for file deleted events

        Returns:
            True on success, False on error
        """
        if self._running:
            logger.warning("Monitor already running")
            return True

        if not self.local_mount_point or not self.local_mount_point.exists():
            logger.error(f"Share path does not exist: {self.local_mount_point}")
            return False

        try:
            event_handler = ShareMonitorEventHandler(
                on_file_added,
                on_file_modified,
                on_file_deleted
            )

            self.observer = Observer()
            self.observer.schedule(event_handler, str(self.local_mount_point), recursive=True)
            self.observer.start()

            self._running = True
            logger.info(f"Started monitoring: {self.local_mount_point}")
            return True

        except Exception as e:
            logger.error(f"Error starting monitor: {e}")
            return False

    def stop(self) -> None:
        """Stop monitoring the network share."""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self._running = False
            logger.info("Stopped monitoring")

    def discover_files(self) -> List[Path]:
        """
        Discover all supported media files in the share.

        Returns:
            List of file paths
        """
        if not self.local_mount_point or not self.local_mount_point.exists():
            logger.error(f"Share path does not exist: {self.local_mount_point}")
            return []

        files = []

        try:
            for file_path in self.local_mount_point.rglob("*"):
                if file_path.is_file() and is_supported_media(file_path):
                    files.append(file_path)

            logger.info(f"Discovered {len(files)} media files")

        except Exception as e:
            logger.error(f"Error discovering files: {e}")

        return files

    @property
    def is_running(self) -> bool:
        """Check if monitor is running."""
        return self._running

    @property
    def is_available(self) -> bool:
        """Check if network share is available."""
        if not self.local_mount_point:
            return False
        return self.local_mount_point.exists()

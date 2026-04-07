"""
API client for communication with the CMS backend.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from ..core.playlist import Playlist, PlaylistItem

logger = logging.getLogger(__name__)


class APIClient:
    """
    Client for communicating with the CMS API.
    """

    def __init__(self, base_url: str, player_id: str, timeout: int = 10):
        """
        Initialize the API client.

        Args:
            base_url: Base URL of the API (e.g., http://localhost:8000/api/v1)
            player_id: Unique ID of this player
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.player_id = player_id
        self.timeout = timeout
        self.client = httpx.AsyncClient(timeout=timeout)
        self._connected = False

    async def heartbeat(
        self,
        status: str,
        current_content_id: Optional[str] = None,
        position: float = 0.0,
        error: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Send heartbeat to the server.

        Args:
            status: Player status (online, playing, paused, error)
            current_content_id: ID of currently playing content
            position: Current playback position in seconds
            error: Error message if any

        Returns:
            Server response with commands, or None on error
        """
        url = f"{self.base_url}/players/{self.player_id}/heartbeat"

        payload = {
            "status": status,
            "current_content_id": current_content_id,
            "position": position,
            "error": error
        }

        try:
            response = await self.client.post(url, json=payload)
            response.raise_for_status()
            self._connected = True
            data = response.json()
            logger.debug(f"Heartbeat sent successfully: {data}")
            return data

        except httpx.HTTPStatusError as e:
            logger.error(f"Heartbeat HTTP error: {e.response.status_code} - {e.response.text}")
            self._connected = False
            return None

        except httpx.RequestError as e:
            logger.error(f"Heartbeat request error: {e}")
            self._connected = False
            return None

        except Exception as e:
            logger.error(f"Heartbeat unexpected error: {e}")
            self._connected = False
            return None

    async def get_assigned_playlist(self) -> Optional[Playlist]:
        """
        Get the playlist assigned to this player.

        Returns:
            Playlist object, or None if none assigned or on error
        """
        url = f"{self.base_url}/players/{self.player_id}/assigned-playlist"

        try:
            response = await self.client.get(url)
            response.raise_for_status()
            data = response.json()
            logger.info(f"Retrieved assigned playlist: {data.get('name')}")

            # Convert API response to Playlist object
            return self._parse_playlist(data)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug("No playlist assigned to this player")
                return None
            logger.error(f"Get playlist HTTP error: {e.response.status_code}")
            return None

        except httpx.RequestError as e:
            logger.error(f"Get playlist request error: {e}")
            return None

        except Exception as e:
            logger.error(f"Get playlist unexpected error: {e}")
            return None

    async def get_player_status(self) -> Optional[Dict[str, Any]]:
        """
        Get the current player status from the server.

        Returns:
            Player status dictionary, or None on error
        """
        url = f"{self.base_url}/players/{self.player_id}/status"

        try:
            response = await self.client.get(url)
            response.raise_for_status()
            return response.json()

        except Exception as e:
            logger.error(f"Get player status error: {e}")
            return None

    async def register_player(self, name: str, location: str, network_share_path: str) -> bool:
        """
        Register this player with the server.

        Args:
            name: Player name
            location: Player location
            network_share_path: Network share path

        Returns:
            True on success, False on error
        """
        url = f"{self.base_url}/players"

        payload = {
            "id": self.player_id,
            "name": name,
            "location": location,
            "network_share_path": network_share_path
        }

        try:
            response = await self.client.post(url, json=payload)
            response.raise_for_status()
            logger.info(f"Player registered successfully: {name}")
            return True

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                logger.debug("Player already registered")
                return True
            logger.error(f"Register player HTTP error: {e.response.status_code}")
            return False

        except Exception as e:
            logger.error(f"Register player error: {e}")
            return False

    def _parse_playlist(self, data: Dict[str, Any]) -> Playlist:
        """
        Parse API playlist response into Playlist object.

        Args:
            data: API response data

        Returns:
            Playlist object
        """
        items = []

        for item_data in data.get("items", []):
            content = item_data.get("content", {})

            # Determine content type
            content_type = content.get("type", "video")

            # For now, use a placeholder path - will be resolved by cache manager
            file_path = Path(f"/tmp/placeholder_{content['id']}")

            item = PlaylistItem(
                id=item_data["id"],
                content_id=item_data["content_id"],
                file_path=file_path,
                duration=item_data.get("duration", 0),
                transition=item_data.get("transition", "cut"),
                order=item_data.get("order", 0),
                content_type=content_type
            )
            items.append(item)

        playlist = Playlist(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            items=items,
            loop=data.get("loop", True)
        )

        return playlist

    @property
    def is_connected(self) -> bool:
        """Check if client is connected to the server."""
        return self._connected

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()
        logger.debug("API client closed")

"""
Command handler for API commands.
"""

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class CommandHandler:
    """
    Handles commands received from the API.
    """

    def __init__(self):
        """Initialize the command handler."""
        self._handlers: Dict[str, Callable] = {}

    def register(self, command: str, handler: Callable) -> None:
        """
        Register a command handler.

        Args:
            command: Command name (e.g., "play", "pause")
            handler: Async function to handle the command
        """
        self._handlers[command] = handler
        logger.debug(f"Registered handler for command: {command}")

    async def handle(self, command: str, params: Optional[Dict[str, Any]] = None) -> bool:
        """
        Handle a command.

        Args:
            command: Command name
            params: Optional command parameters

        Returns:
            True if command was handled, False otherwise
        """
        if command == "none" or not command:
            return True

        handler = self._handlers.get(command)

        if handler:
            try:
                logger.info(f"Handling command: {command}")
                if params:
                    await handler(**params)
                else:
                    await handler()
                return True

            except Exception as e:
                logger.error(f"Error handling command '{command}': {e}")
                return False
        else:
            logger.warning(f"No handler registered for command: {command}")
            return False

    def has_handler(self, command: str) -> bool:
        """
        Check if a handler is registered for a command.

        Args:
            command: Command name

        Returns:
            True if handler exists, False otherwise
        """
        return command in self._handlers

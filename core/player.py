"""
Media player engine using mpv.

Provides MpvPlayer — a thin wrapper used by main.py — and the legacy
MediaPlayer / PlayerState classes for any modules that still import them.
"""

import asyncio
import logging
from pathlib import Path
from typing import Callable, Optional

try:
    import mpv as _mpv_module
except ImportError:
    _mpv_module = None

from .playlist import Playlist, PlaylistItem

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PlayerState — kept for backward compatibility
# ---------------------------------------------------------------------------

class PlayerState:
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"
    ERROR = "error"


# ---------------------------------------------------------------------------
# MpvPlayer — the interface used by main.py
# ---------------------------------------------------------------------------

class MpvPlayer:
    """
    Thin wrapper around python-mpv.

    mpv event callbacks fire on mpv's internal thread; we schedule
    coroutines back onto the asyncio event loop via call_soon_threadsafe()
    so all state mutations happen safely on the loop.
    """

    def __init__(self, display: str = ":0", audio_output: str = "auto"):
        if _mpv_module is None:
            raise RuntimeError(
                "python-mpv is not installed.  Run: pip install python-mpv"
            )
        self.display = display
        self.audio_output = audio_output
        self._mpv: Optional[_mpv_module.MPV] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._eof_callback = None   # async callable() — invoked on end-of-file

    def initialise(self, loop: asyncio.AbstractEventLoop) -> None:
        """Create the mpv instance.  Must be called from the async main."""
        self._loop = loop

        kwargs = dict(
            input_default_bindings=False,
            input_vo_keyboard=False,
            osc=False,
            ytdl=False,
            keep_open="no",
            force_window="yes",
            vo="gpu",
            hwdec="auto",
            fs=True,
            really_quiet=True,
        )

        if self.audio_output and self.audio_output != "auto":
            kwargs["audio_device"] = self.audio_output

        self._mpv = _mpv_module.MPV(**kwargs)

        @self._mpv.event_callback("end-file")
        def _on_end_file(event):
            # Fire for natural end-of-file only
            reason = event.get("reason", None) if isinstance(event, dict) else None
            if reason in (None, "eof", 0):
                if self._eof_callback and self._loop:
                    self._loop.call_soon_threadsafe(
                        self._loop.create_task, self._eof_callback()
                    )

        logger.info("MpvPlayer initialised (fullscreen, gpu vo)")

    def set_eof_callback(self, coro_fn) -> None:
        """Register an async callable to invoke when a file ends naturally."""
        self._eof_callback = coro_fn

    def play_file(self, path: str) -> None:
        """Load and play a local file path (or URL)."""
        if self._mpv:
            self._mpv.play(path)

    def show_osd(self, text: str, duration_ms: int = 10_000) -> None:
        """Display OSD text overlay."""
        if self._mpv:
            try:
                self._mpv.command("show-text", text, str(duration_ms))
            except Exception:
                try:
                    self._mpv.osd_msg1 = text
                except Exception:
                    pass

    def cmd_play(self) -> None:
        if self._mpv:
            self._mpv.pause = False

    def cmd_pause(self) -> None:
        if self._mpv:
            self._mpv.pause = True

    def cmd_stop(self) -> None:
        if self._mpv:
            try:
                self._mpv.command("stop")
            except Exception:
                pass

    def cmd_seek_start(self) -> None:
        """Seek to start of current file (restart)."""
        if self._mpv:
            try:
                self._mpv.command("seek", "0", "absolute")
            except Exception:
                pass

    def terminate(self) -> None:
        if self._mpv:
            try:
                self._mpv.terminate()
            except Exception:
                pass
            self._mpv = None


# ---------------------------------------------------------------------------
# MediaPlayer — legacy class retained for backward compatibility
# ---------------------------------------------------------------------------

class MediaPlayer:
    """
    Legacy media player interface.  New code should use MpvPlayer directly.

    This class remains for any existing callers that import it.
    """

    def __init__(self, display: str = ":0", audio_output: str = "auto"):
        if _mpv_module is None:
            raise RuntimeError("python-mpv is not installed")

        self.display = display
        self.audio_output = audio_output
        self.mpv_instance: Optional[_mpv_module.MPV] = None
        self.current_playlist: Optional[Playlist] = None
        self.current_index: int = -1
        self.state: str = PlayerState.STOPPED
        self.error_message: Optional[str] = None
        self.position: float = 0.0
        self.duration: float = 0.0

        self._on_item_changed: Optional[Callable[[PlaylistItem], None]] = None
        self._on_state_changed: Optional[Callable[[str], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None

        self._image_timer: Optional[asyncio.Task] = None
        self._is_image_playing: bool = False

    def initialize(self) -> None:
        """Initialize the mpv player."""
        try:
            kwargs = dict(
                input_default_bindings=True,
                input_vo_keyboard=True,
                osc=True,
                ytdl=False,
                keep_open="yes",
                force_window="yes",
                vo="gpu",
                hwdec="auto",
            )
            if self.audio_output and self.audio_output != "auto":
                kwargs["audio_device"] = self.audio_output

            self.mpv_instance = _mpv_module.MPV(**kwargs)

            @self.mpv_instance.event_callback("end-file")
            def on_end_file(event):
                if not self._is_image_playing:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.create_task(self._on_playback_finished())

            @self.mpv_instance.property_observer("time-pos")
            def on_time_pos(name, value):
                if value is not None:
                    self.position = value

            @self.mpv_instance.property_observer("duration")
            def on_duration(name, value):
                if value is not None:
                    self.duration = value

            logger.info("MediaPlayer initialized")

        except Exception as e:
            logger.error(f"Failed to initialize mpv: {e}")
            self.state = PlayerState.ERROR
            self.error_message = str(e)
            raise

    # -- Playback control --

    async def load_playlist(self, playlist: Playlist) -> None:
        logger.info(f"Loading playlist: {playlist.name} ({len(playlist)} items)")
        self.current_playlist = playlist
        self.current_index = -1
        self.state = PlayerState.STOPPED
        if len(playlist) > 0:
            await self.play()

    async def play(self) -> None:
        if self.state == PlayerState.PAUSED:
            if self.mpv_instance:
                self.mpv_instance.pause = False
            self.state = PlayerState.PLAYING
            self._trigger_state_changed()
        else:
            if self.current_index == -1:
                await self.next()
            else:
                if self.mpv_instance:
                    self.mpv_instance.pause = False
                self.state = PlayerState.PLAYING
                self._trigger_state_changed()

    async def pause(self) -> None:
        if self.mpv_instance and self.state == PlayerState.PLAYING:
            self.mpv_instance.pause = True
            self.state = PlayerState.PAUSED
            self._trigger_state_changed()
            if self._image_timer and not self._image_timer.done():
                self._image_timer.cancel()

    async def stop(self) -> None:
        if self.mpv_instance:
            self.mpv_instance.command("stop")
        self.state = PlayerState.STOPPED
        self.current_index = -1
        self.position = 0.0
        self._trigger_state_changed()
        if self._image_timer and not self._image_timer.done():
            self._image_timer.cancel()

    async def next(self) -> None:
        if not self.current_playlist or len(self.current_playlist) == 0:
            return
        next_index = self.current_index + 1
        if next_index >= len(self.current_playlist):
            if self.current_playlist.loop:
                next_index = 0
            else:
                await self.stop()
                return
        await self._play_item(next_index)

    async def previous(self) -> None:
        if not self.current_playlist or len(self.current_playlist) == 0:
            return
        prev_index = self.current_index - 1
        if prev_index < 0:
            if self.current_playlist.loop:
                prev_index = len(self.current_playlist) - 1
            else:
                return
        await self._play_item(prev_index)

    async def _play_item(self, index: int) -> None:
        if not self.current_playlist:
            return
        item = self.current_playlist[index]
        logger.info(f"Playing item {index+1}/{len(self.current_playlist)}: {item.file_path.name}")

        if self._image_timer and not self._image_timer.done():
            self._image_timer.cancel()
        self._is_image_playing = False

        try:
            if not item.file_path.exists():
                raise FileNotFoundError(f"Media file not found: {item.file_path}")
            self.current_index = index
            self.position = 0.0
            if item.is_image:
                self._play_image(item)
            else:
                self._play_video(item)
            self.state = PlayerState.PLAYING
            self._trigger_item_changed(item)
            self._trigger_state_changed()
        except Exception as e:
            logger.error(f"Error playing item: {e}")
            self.state = PlayerState.ERROR
            self.error_message = str(e)
            self._trigger_error(str(e))
            await asyncio.sleep(1)
            await self.next()

    def _play_video(self, item: PlaylistItem) -> None:
        if self.mpv_instance:
            self.mpv_instance.play(str(item.file_path))

    def _play_image(self, item: PlaylistItem) -> None:
        if self.mpv_instance:
            self.mpv_instance.play(str(item.file_path))
            self._is_image_playing = True
            duration = item.effective_duration or 10.0
            self._image_timer = asyncio.create_task(
                self._image_display_timer(duration)
            )

    async def _image_display_timer(self, duration: float) -> None:
        try:
            await asyncio.sleep(duration)
            await self._on_playback_finished()
        except asyncio.CancelledError:
            pass

    async def _on_playback_finished(self) -> None:
        self._is_image_playing = False
        await self.next()

    def get_current_item(self) -> Optional[PlaylistItem]:
        if self.current_playlist and 0 <= self.current_index < len(self.current_playlist):
            return self.current_playlist[self.current_index]
        return None

    def set_on_item_changed(self, callback: Callable[[PlaylistItem], None]) -> None:
        self._on_item_changed = callback

    def set_on_state_changed(self, callback: Callable[[str], None]) -> None:
        self._on_state_changed = callback

    def set_on_error(self, callback: Callable[[str], None]) -> None:
        self._on_error = callback

    def _trigger_item_changed(self, item: PlaylistItem) -> None:
        if self._on_item_changed:
            self._on_item_changed(item)

    def _trigger_state_changed(self) -> None:
        if self._on_state_changed:
            self._on_state_changed(self.state)

    def _trigger_error(self, error: str) -> None:
        if self._on_error:
            self._on_error(error)

    def cleanup(self) -> None:
        logger.info("Cleaning up MediaPlayer")
        if self._image_timer and not self._image_timer.done():
            self._image_timer.cancel()
        if self.mpv_instance:
            try:
                self.mpv_instance.terminate()
            except Exception as e:
                logger.error(f"Error terminating mpv: {e}")
            self.mpv_instance = None

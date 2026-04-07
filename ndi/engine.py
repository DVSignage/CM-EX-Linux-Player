"""
NDI Engine for the Linux signage player.

Provides NDI source discovery, receive (fullscreen display via OpenCV),
and send (broadcast player output as NDI source).

Gracefully disabled if ndi-python or opencv are not installed.
"""

import asyncio
import logging
import threading
import time
from typing import Optional, List, Dict, Callable

log = logging.getLogger("player.ndi")

# ---------------------------------------------------------------------------
# Optional imports — NDI features disabled if not available
# ---------------------------------------------------------------------------
try:
    import NDIlib as ndi

    _NDI_AVAILABLE = ndi.initialize()
    if _NDI_AVAILABLE:
        log.info("[NDI] ndi-python loaded and initialised successfully")
    else:
        log.warning("[NDI] ndi.initialize() failed — NDI features disabled")
except ImportError:
    ndi = None
    _NDI_AVAILABLE = False
    log.info("[NDI] ndi-python not installed — NDI features disabled")

try:
    import numpy as np
except ImportError:
    np = None

try:
    import cv2
except ImportError:
    cv2 = None


def is_available() -> bool:
    """Check if NDI support is available."""
    return _NDI_AVAILABLE and np is not None


# ---------------------------------------------------------------------------
# NDI Source Discovery
# ---------------------------------------------------------------------------

def find_sources(timeout_ms: int = 3000) -> List[Dict[str, str]]:
    """
    Scan for NDI sources on the network.
    Returns list of {name, url} dicts.
    """
    if not is_available():
        log.warning("[NDI] find_sources called but NDI is not available")
        return []

    ndi_find = ndi.find_create_v2()
    if ndi_find is None:
        log.error("[NDI] Failed to create finder")
        return []

    try:
        ndi.find_wait_for_sources(ndi_find, timeout_ms)
        sources = ndi.find_get_current_sources(ndi_find)
        result = []
        for s in sources:
            result.append({
                "name": s.ndi_name,
                "url": getattr(s, "url_address", ""),
            })
        log.info(f"[NDI] Found {len(result)} source(s): {[s['name'] for s in result]}")
        return result
    except Exception as e:
        log.error(f"[NDI] Source scan error: {e}")
        return []
    finally:
        ndi.find_destroy(ndi_find)


# ---------------------------------------------------------------------------
# NDI Receiver — displays fullscreen via OpenCV
# ---------------------------------------------------------------------------

class NDIReceiver:
    """
    Receives video from an NDI source and displays it fullscreen.

    Runs the receive/display loop on a background thread so it doesn't
    block the asyncio event loop.
    """

    # How long to wait (seconds) with no frames before showing no-signal
    NO_SIGNAL_DELAY = 20.0
    WINDOW_NAME = "NDI Player"

    def __init__(self):
        self._recv = None
        self._active = False
        self._thread: Optional[threading.Thread] = None
        self._source_name: str = ""
        self._crop: Optional[Dict] = None  # {x, y, w, h} normalised 0–1
        self._on_no_signal: Optional[Callable] = None
        self._on_signal_restored: Optional[Callable] = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def source_name(self) -> str:
        return self._source_name

    def start(
        self,
        source_name: str,
        crop: Optional[Dict] = None,
        on_no_signal: Optional[Callable] = None,
        on_signal_restored: Optional[Callable] = None,
    ) -> bool:
        """
        Start receiving from an NDI source.
        Returns True if source was found and receiver started.
        """
        if not is_available():
            log.warning("[NDI] Cannot start receiver — NDI not available")
            return False

        if cv2 is None:
            log.warning("[NDI] Cannot start receiver — OpenCV not installed")
            return False

        # Stop any existing receiver first
        self.stop()

        log.info(f"[NDI] Starting receiver for: {source_name}")
        self._source_name = source_name
        self._crop = crop
        self._on_no_signal = on_no_signal
        self._on_signal_restored = on_signal_restored

        # Find the source
        ndi_find = ndi.find_create_v2()
        if ndi_find is None:
            log.error("[NDI] Failed to create finder")
            return False

        try:
            ndi.find_wait_for_sources(ndi_find, 3000)
            sources = ndi.find_get_current_sources(ndi_find)
            source = None
            for s in sources:
                if source_name in s.ndi_name:
                    source = s
                    break

            if source is None:
                log.warning(
                    f"[NDI] Source not found: '{source_name}'. "
                    f"Available: {[s.ndi_name for s in sources]}"
                )
                ndi.find_destroy(ndi_find)
                return False

            # Create receiver
            recv_create = ndi.RecvCreateV3()
            recv_create.color_format = ndi.RECV_COLOR_FORMAT_BGRX_BGRA
            recv_create.bandwidth = ndi.RECV_BANDWIDTH_HIGHEST

            self._recv = ndi.recv_create_v3(recv_create)
            if self._recv is None:
                log.error("[NDI] Failed to create receiver")
                ndi.find_destroy(ndi_find)
                return False

            ndi.recv_connect(self._recv, source)
            log.info(f"[NDI] Connected to: {source.ndi_name}")

        finally:
            ndi.find_destroy(ndi_find)

        # Start receive/display loop on background thread
        self._active = True
        self._thread = threading.Thread(
            target=self._receive_loop, daemon=True, name="ndi-recv"
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop receiving and close the display window."""
        if not self._active:
            return

        log.info(f"[NDI] Stopping receiver for: {self._source_name}")
        self._active = False

        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

        if self._recv:
            try:
                ndi.recv_destroy(self._recv)
            except Exception:
                pass
            self._recv = None

        # Close OpenCV window
        try:
            cv2.destroyWindow(self.WINDOW_NAME)
        except Exception:
            pass

    def set_crop(self, crop: Optional[Dict]) -> None:
        """Update crop region. crop = {x, y, w, h} normalised 0–1, or None."""
        self._crop = crop

    def _receive_loop(self) -> None:
        """Background thread: receive NDI frames and display via OpenCV."""
        last_frame_time = time.monotonic()
        no_signal_shown = False
        frame_count = 0

        # Create fullscreen window
        try:
            cv2.namedWindow(self.WINDOW_NAME, cv2.WINDOW_NORMAL)
            cv2.setWindowProperty(
                self.WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN
            )
        except Exception as e:
            log.error(f"[NDI] Failed to create display window: {e}")
            self._active = False
            return

        while self._active:
            try:
                t, v, a, m = ndi.recv_capture_v2(self._recv, 100)  # 100ms timeout

                if t == ndi.FRAME_TYPE_VIDEO:
                    frame = np.copy(v.data)
                    ndi.recv_free_video_v2(self._recv, v)

                    last_frame_time = time.monotonic()
                    frame_count += 1

                    if no_signal_shown:
                        no_signal_shown = False
                        log.info(f"[NDI] Signal restored for: {self._source_name}")
                        if self._on_signal_restored:
                            try:
                                self._on_signal_restored()
                            except Exception:
                                pass

                    # Apply crop if set
                    display_frame = self._apply_crop(frame)

                    # Convert BGRX → BGR (drop alpha channel)
                    if display_frame.shape[2] == 4:
                        display_frame = display_frame[:, :, :3]

                    cv2.imshow(self.WINDOW_NAME, display_frame)

                elif t == ndi.FRAME_TYPE_AUDIO and a is not None:
                    ndi.recv_free_audio_v2(self._recv, a)

                elif t == ndi.FRAME_TYPE_METADATA and m is not None:
                    ndi.recv_free_metadata(self._recv, m)

                # Check for no-signal timeout
                silent = time.monotonic() - last_frame_time
                if not no_signal_shown and silent >= self.NO_SIGNAL_DELAY:
                    no_signal_shown = True
                    log.warning(
                        f"[NDI] No frames for {silent:.0f}s on "
                        f"'{self._source_name}' — no signal"
                    )
                    if self._on_no_signal:
                        try:
                            self._on_no_signal()
                        except Exception:
                            pass

                # OpenCV event pump (1ms wait) — also allows window close
                if cv2.waitKey(1) & 0xFF == 27:  # ESC to close
                    break

            except Exception as e:
                log.error(f"[NDI] Receive error: {e}")
                time.sleep(0.1)

        log.info(
            f"[NDI] Receive loop ended for: {self._source_name} "
            f"(total frames: {frame_count})"
        )

        # Cleanup window
        try:
            cv2.destroyWindow(self.WINDOW_NAME)
        except Exception:
            pass

    def _apply_crop(self, frame: "np.ndarray") -> "np.ndarray":
        """Apply normalised crop {x, y, w, h} to a frame."""
        if not self._crop:
            return frame

        h, w = frame.shape[:2]
        cx = self._crop.get("x", 0)
        cy = self._crop.get("y", 0)
        cw = self._crop.get("w", 1)
        ch = self._crop.get("h", 1)

        x1 = int(cx * w)
        y1 = int(cy * h)
        x2 = int((cx + cw) * w)
        y2 = int((cy + ch) * h)

        # Clamp
        x1 = max(0, min(x1, w))
        y1 = max(0, min(y1, h))
        x2 = max(x1 + 1, min(x2, w))
        y2 = max(y1 + 1, min(y2, h))

        return frame[y1:y2, x1:x2]


# ---------------------------------------------------------------------------
# NDI Sender — broadcasts frames as an NDI source
# ---------------------------------------------------------------------------

class NDISender:
    """
    Broadcasts video frames as an NDI source.
    Useful for outputting the player's content as an NDI stream.
    """

    def __init__(self):
        self._send = None
        self._active = False
        self._source_name: str = ""

    @property
    def active(self) -> bool:
        return self._active

    @property
    def source_name(self) -> str:
        return self._source_name

    def start(self, source_name: str) -> bool:
        """Start broadcasting as an NDI source."""
        if not is_available():
            log.warning("[NDI SEND] Cannot start — NDI not available")
            return False

        self.stop()

        try:
            send_create = ndi.SendCreate()
            send_create.ndi_name = source_name
            send_create.clock_video = False
            send_create.clock_audio = False

            self._send = ndi.send_create(send_create)
            if self._send is None:
                log.error("[NDI SEND] Failed to create sender")
                return False

            self._active = True
            self._source_name = source_name
            log.info(f"[NDI SEND] Broadcasting as: {source_name}")
            return True

        except Exception as e:
            log.error(f"[NDI SEND] Failed to create sender: {e}")
            return False

    def send_frame(self, frame: "np.ndarray") -> None:
        """
        Send a single video frame.
        frame: numpy array of shape (height, width, 4) dtype=uint8, BGRA format.
        """
        if not self._active or not self._send:
            return

        try:
            video_frame = ndi.VideoFrameV2()
            video_frame.data = frame
            video_frame.FourCC = ndi.FOURCC_VIDEO_TYPE_BGRA
            ndi.send_send_video_v2(self._send, video_frame)
        except Exception as e:
            log.error(f"[NDI SEND] Frame send error: {e}")

    def stop(self) -> None:
        """Stop broadcasting."""
        if not self._active:
            return

        log.info(f"[NDI SEND] Stopping broadcast: {self._source_name}")
        self._active = False

        if self._send:
            try:
                ndi.send_destroy(self._send)
            except Exception:
                pass
            self._send = None

    def __del__(self):
        self.stop()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup():
    """Call on application shutdown to release NDI resources."""
    if _NDI_AVAILABLE:
        try:
            ndi.destroy()
            log.info("[NDI] Shutdown complete")
        except Exception:
            pass

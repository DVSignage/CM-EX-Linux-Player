"""
Video wall: crop filters and the server-streamed wall (`load_wall_rtp`).

Two wall models are supported:
  - `load_wall_content` (DVSI Plus): the player downloads the content and
    shows its own region; the crop is normalised 0..1 (pixel values with
    canvas_w/canvas_h are normalised first);
  - `load_wall_rtp` (Linux player): the server streams the whole wall over
    multicast and each player crops its region (pixel crop with canvas).
"""

from typing import Optional, Tuple

# mpv options tuned for low-latency multicast wall streams.
STREAM_TUNING = {
    "demuxer-lavf-probesize": 131072,
    "demuxer-lavf-analyzeduration": 0.5,
    "cache": "no",
    "profile": "low-latency",
    "video-sync": "audio",
    "audio-buffer": 0,
    "keep-open": "no",
}
UDP_BUFFER_PARAMS = "buffer_size=16777216&fifo_size=50000000&overrun_nonfatal=1"


def _num(d: dict, key: str) -> float:
    try:
        return float(d.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def normalise_crop(crop: Optional[dict]) -> Optional[Tuple[float, float, float, float]]:
    """(x, y, w, h) in 0..1 from a wall crop. With canvas_w/canvas_h > 0 the
    values are pixels and are divided by the canvas; otherwise they are taken
    as already normalised. None for no crop or an empty region."""
    if not isinstance(crop, dict):
        return None
    x, y, w, h = (_num(crop, k) for k in ("x", "y", "w", "h"))
    cw, ch = _num(crop, "canvas_w"), _num(crop, "canvas_h")
    if cw > 0 and ch > 0:
        x, y, w, h = x / cw, y / ch, w / cw, h / ch
    if w <= 0 or h <= 0:
        return None
    clamp = lambda v: max(0.0, min(1.0, v))
    x, y = clamp(x), clamp(y)
    w, h = min(w, 1.0 - x), min(h, 1.0 - y)
    if w <= 0 or h <= 0:
        return None
    if (x, y, w, h) == (0.0, 0.0, 1.0, 1.0):
        return None
    return round(x, 6), round(y, 6), round(w, 6), round(h, 6)


def crop_filter_normalised(crop: Optional[Tuple[float, float, float, float]]) -> Optional[str]:
    """mpv/lavfi crop of a normalised region; mpv then stretches it full screen
    (keepaspect is turned off while a crop is set)."""
    if not crop:
        return None
    x, y, w, h = crop
    return f"crop=w=iw*{w:g}:h=ih*{h:g}:x=iw*{x:g}:y=ih*{y:g},setsar=1"


def crop_filter_pixel(crop: Optional[dict]) -> Optional[str]:
    """`wall_crop {x, y, w, h, canvas_w, canvas_h}` in pixels → mpv filter.
    Scales the source to the canvas (no letterbox), resets SAR, then crops
    this display's part."""
    if not crop:
        return None
    w, h = int(_num(crop, "w")), int(_num(crop, "h"))
    x, y = int(_num(crop, "x")), int(_num(crop, "y"))
    cw, ch = int(_num(crop, "canvas_w")), int(_num(crop, "canvas_h"))
    if w > 0 and h > 0:
        if cw > 0 and ch > 0:
            return (f"scale={cw}:{ch}:force_original_aspect_ratio=disable"
                    f",setsar=1,crop={w}:{h}:{x}:{y}")
        return f"crop={w}:{h}:{x}:{y}"
    return None


def stream_url_with_buffers(url: str) -> str:
    return url + ("&" if "?" in url else "?") + UDP_BUFFER_PARAMS

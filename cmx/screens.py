"""
Full-screen player screens rendered to PNG with Pillow and shown by mpv:
registration (code + Connect:EX QR), connecting/offline, waiting for content.

Branding follows the DVSI Plus registration screens: black, white and
Caribbean Green #16EB87.
"""

import logging
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("player")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:   # screens fall back to mpv OSD text
    Image = ImageDraw = ImageFont = None

try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M
except ImportError:
    qrcode = None

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GREEN = (0x16, 0xEB, 0x87)
GREY = (150, 150, 150)

_BOLD_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
]
_REGULAR_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "DejaVuSans.ttf",
]


def available() -> bool:
    return Image is not None


def _font(size: int, bold: bool = False):
    for path in (_BOLD_FONTS if bold else _REGULAR_FONTS):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _centered(draw, y: int, text: str, font, fill, width: int, x0: int = 0) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    draw.text((x0 + (width - w) // 2, y), text, font=font, fill=fill)
    return y + h


def qr_image(text: str, size_px: int):
    """A QR code (error correction M, 4-module quiet zone), dark on white."""
    if qrcode is None or Image is None:
        return None
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=4, box_size=1)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("L")
    # nearest-neighbour keeps the modules crisp at any size
    return img.resize((size_px, size_px), Image.NEAREST)


def render_enrollment(path: Path, size: Tuple[int, int], code: str, server: str,
                      site_name: Optional[str], link: Optional[str]) -> bool:
    if Image is None:
        return False
    w, h = size
    s = h / 1080.0
    img = Image.new("RGB", (w, h), BLACK)
    d = ImageDraw.Draw(img)

    qr = qr_image(link, int(300 * s)) if link else None
    text_w = int(w * 0.55) if qr is not None else w
    y = int(h * 0.26)
    y = _centered(d, y, "REGISTRATION CODE", _font(int(44 * s), True), WHITE, text_w) + int(40 * s)
    y = _centered(d, y, code, _font(int(150 * s), True), GREEN, text_w) + int(50 * s)
    y = _centered(d, y, "Enter this code in CM:EX to approve this player", _font(int(34 * s)), WHITE, text_w) + int(24 * s)
    y = _centered(d, y, server, _font(int(28 * s)), GREY, text_w) + int(14 * s)
    if site_name:
        y = _centered(d, y, f"Site: {site_name}", _font(int(28 * s)), GREY, text_w) + int(14 * s)
    _centered(d, y + int(30 * s), "Waiting for approval…", _font(int(28 * s)), GREY, text_w)

    if qr is not None:
        pad = int(24 * s)
        card_w = qr.width + 2 * pad
        cx = text_w + (w - text_w - card_w) // 2
        cy = (h - card_w) // 2 - int(30 * s)
        d.rounded_rectangle([cx, cy, cx + card_w, cy + card_w], radius=int(18 * s), fill=WHITE)
        img.paste(qr, (cx + pad, cy + pad))
        _centered(d, cy + card_w + int(24 * s), "Or scan with Connect:EX", _font(int(28 * s)), WHITE, card_w, cx)

    return _save(img, path)


def render_message(path: Path, size: Tuple[int, int], title: str, body: str = "") -> bool:
    if Image is None:
        return False
    w, h = size
    s = h / 1080.0
    img = Image.new("RGB", (w, h), BLACK)
    d = ImageDraw.Draw(img)
    y = _centered(d, int(h * 0.42), title, _font(int(60 * s), True), WHITE, w) + int(36 * s)
    if body:
        _centered(d, y, body, _font(int(32 * s)), GREY, w)
    return _save(img, path)


def render_black(path: Path) -> bool:
    if Image is None:
        return False
    return _save(Image.new("RGB", (16, 16), BLACK), path)


def _save(img, path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, "PNG")
        return True
    except OSError as e:
        log.warning(f"[SCREEN] could not write {path.name}: {e}")
        return False

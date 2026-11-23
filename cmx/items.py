"""
Playlist items: what each CMS playlist entry is and how long it stays up.

Classification follows DVSI Plus `CmxPlayerService` (IsTemplateItem,
IsWebItem, WebUrlFor, MediaDwellMs, ClipBound) and `Cmx/CmxItem.cs`:

  template                         -> {base}/api/v1/templates/{tid}/render
  url/webpage/iframe/link/embed/web -> content.url (else the stream URL)
  rss                              -> {base}/api/v1/rss/{cid}/render, never
                                      content.url (the publisher's raw XML)
  anything else                    -> media, cached as {content_id}{ext}
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

VIDEO, IMAGE, WEB, TEMPLATE = "video", "image", "web", "template"

WEB_TYPES = {"url", "webpage", "iframe", "link", "embed", "web", "rss"}
WEB_DEFAULT_DWELL = 30.0

_IMAGE_NAME = re.compile(r"\.(png|jpe?g|gif|webp|bmp|svg)$", re.IGNORECASE)
_AUDIO_NAME = re.compile(r"\.(mp3|wav|m4a|aac|ogg|oga|flac|wma)$", re.IGNORECASE)
_TEMPLATE_ID = re.compile(r"/templates/([^/?#]+)/render", re.IGNORECASE)


@dataclass
class Item:
    kind: str                          # video | image | web | template
    content_id: str
    name: str
    duration: float = 0.0              # dwell seconds; 0 = per-kind default / play to end
    path: Optional[Path] = None        # media: local cache path
    url: Optional[str] = None          # web/template: page to show
    clip_start: Optional[float] = None
    clip_end: Optional[float] = None
    template_id: Optional[str] = None
    canvas: tuple = (0, 0)             # template render canvas (w, h); (0, 0) = responsive
    extra: dict = field(default_factory=dict)

    @property
    def is_media(self) -> bool:
        return self.kind in (VIDEO, IMAGE)

    @property
    def is_clipped(self) -> bool:
        return self.clip_start is not None or self.clip_end is not None

    @property
    def is_audio(self) -> bool:
        return self.kind == VIDEO and bool(_AUDIO_NAME.search(self.name or ""))

    def signature(self) -> str:
        """Identity for "is this the same content?". The raw clip bounds are in
        it, exactly as the CMS folds them into playlist_hash: a window moved
        10→20 ⇒ 30→40 is still 10 s long but is different content."""
        return "|".join([
            self.kind, str(self.path or self.url or ""), f"{self.duration:g}",
            f"{self.canvas[0]}x{self.canvas[1]}",
            f"{self.clip_start!r}→{self.clip_end!r}", self.content_id,
        ])

    def dwell(self, default_image: float) -> float:
        """Seconds before the item advances on a timer; 0 = wait for the media
        to end. Video never uses a timer: its cut-off is the out-point, which
        mpv enforces on the playhead (a wall-clock timer would start before the
        file opened and cut the clip short)."""
        if self.kind == IMAGE:
            return self.duration if self.duration > 0 else default_image
        if self.kind in (WEB, TEMPLATE):
            return self.duration if self.duration > 0 else WEB_DEFAULT_DWELL
        return 0.0

    @property
    def in_point(self) -> float:
        return self.clip_start or 0.0

    @property
    def out_point(self) -> Optional[float]:
        """Where a video stops, in source seconds: the clip end, capped by an
        operator dwell measured from the in-point; None = the natural end."""
        if self.kind != VIDEO:
            return None
        end = self.clip_end
        if self.duration > 0:
            cap = self.in_point + self.duration
            end = cap if end is None else min(end, cap)
        return end


def same_content(a: Optional[List[Item]], b: Optional[List[Item]]) -> bool:
    """Exactly the same entries in the same order. Empty lists are never the
    same: an empty update is the explicit "release the players" signal."""
    if not a or not b or len(a) != len(b):
        return False
    return all(x.signature() == y.signature() for x, y in zip(a, b))


def content_of(item: dict) -> dict:
    c = item.get("content")
    return c if isinstance(c, dict) else {}


def content_type(item: dict) -> str:
    return str(content_of(item).get("type") or "").strip().lower()


def is_template_item(item: dict) -> bool:
    return content_type(item) == "template"


def is_rss_item(item: dict) -> bool:
    return content_type(item) == "rss"


def is_web_item(item: dict) -> bool:
    return content_type(item) in WEB_TYPES


def is_image_name(name: str) -> bool:
    return bool(_IMAGE_NAME.search(name or ""))


def template_id_from(item: dict) -> Optional[str]:
    """The template id from content.url (`.../templates/{id}/render`)."""
    url = str(content_of(item).get("url") or "")
    m = _TEMPLATE_ID.search(url)
    if m:
        return m.group(1)
    for key in ("template_id", "templateId"):
        v = content_of(item).get(key) or item.get(key)
        if v:
            return str(v)
    return None


def file_name_for(item: dict) -> str:
    fn = content_of(item).get("filename")
    if fn:
        return str(fn)
    return f"{item.get('content_id')}.mp4"


def cache_name(content_id: str, filename: str) -> str:
    """The cache key: content id plus the source extension. Keying on the
    CMS filename collided two contents both called promo.mp4."""
    ext = Path(filename).suffix.lower() if filename else ""
    return f"{content_id}{ext or '.mp4'}"


def item_duration(item: dict) -> float:
    try:
        return max(0.0, float(item.get("duration") or 0))
    except (TypeError, ValueError):
        return 0.0


def media_dwell(duration: float, natural: Optional[float]) -> float:
    """A video's dwell: CM:EX auto-fills `duration` from the clip's own length
    (83 for an 82.96 s clip), so only a duration more than 1 s SHORTER than
    the media is an operator cut-off. 0 = play to the natural end."""
    if duration <= 0:
        return 0.0
    if natural is not None and natural > 0 and duration >= natural - 1.0:
        return 0.0
    return duration


def clip_bound(item: dict, key: str) -> Optional[float]:
    """`clip_start`/`clip_end` in seconds; tolerates a numeric string, ignores
    a negative or unparseable value."""
    raw = item.get(key)
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def classify(item: dict, cms, cache_dir: Path) -> Optional[Item]:
    """One playlist entry → Item, or None when it has no content id.
    `cms` provides the URL builders (CmsClient)."""
    cid = item.get("content_id")
    if not cid:
        return None
    cid = str(cid)
    duration = item_duration(item)
    content = content_of(item)
    name = str(content.get("name") or content.get("filename") or cid)

    if is_template_item(item):
        tid = template_id_from(item)
        url = cms.template_render_url(tid) if tid else cms.stream_url(cid)
        return Item(kind=TEMPLATE, content_id=cid, name=name, duration=duration,
                    url=url, template_id=tid)

    if is_web_item(item):
        if is_rss_item(item):
            url = cms.rss_render_url(cid)
        else:
            url = str(content.get("url") or "").strip() or cms.stream_url(cid)
        return Item(kind=WEB, content_id=cid, name=name, duration=duration, url=url)

    src = file_name_for(item)
    path = cache_dir / cache_name(cid, src)
    if is_image_name(src):
        return Item(kind=IMAGE, content_id=cid, name=src, duration=duration, path=path)

    natural = content.get("duration")
    try:
        natural = float(natural) if natural is not None else None
    except (TypeError, ValueError):
        natural = None
    start, end = clip_bound(item, "clip_start"), clip_bound(item, "clip_end")
    if end is not None and end <= (start or 0.0):
        end = None   # an inverted window is ignored
    return Item(kind=VIDEO, content_id=cid, name=src, duration=media_dwell(duration, natural),
                path=path, clip_start=start, clip_end=end)

"""
Native templates: a CMS template definition → what the renderer page draws.

Follows DVSI Plus `TemplateRenderer` / `NestedTemplates` / `TemplatePatcher`:
  - element types text, panel, image, video, capture, rss, trigger_zone and
    template (nested); any other type anywhere → the CMS web render instead;
  - an under-configured element of a known type is an empty box, never a
    reason to fall back;
  - nested templates up to 3 levels deep, cycles cut, a child that cannot be
    fetched is an empty box;
  - every content id the template (and its interactive wall) can show is
    downloaded first, so nothing waits on the network once it is up;
  - `template_patch` changes one element and the cached definition.
"""

import copy
import hashlib
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional, Set, Tuple

from . import cache as cachemod
from .interaction import Model
from .wallhost import canonical_keys

log = logging.getLogger("player")

KNOWN_TYPES = {"text", "panel", "image", "video", "capture", "rss", "trigger_zone", "template", "web"}
PATCHABLE = {"image", "video", "text", "capture", "rss"}
MEDIA_KEYS = ("contentId", "content_id", "contentID", "mediaId", "media_id", "assetId", "asset_id")
DEVICE_KEYS = ("deviceLabel", "device_label", "deviceId", "device_id", "label", "capture_device_label")
AUDIO_KEYS = ("audioEnabled", "audio", "audio_enabled", "enableAudio", "emit_audio")
MAX_DEPTH = 3

FetchDef = Callable[[str], Awaitable[Optional[dict]]]
EnsureMedia = Callable[[str, str], Awaitable[Optional[Path]]]


def media_id(props: dict) -> Optional[str]:
    for k in MEDIA_KEYS:
        v = props.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def device_label(props: dict) -> Optional[str]:
    for k in DEVICE_KEYS:
        v = props.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def capture_audio(props: dict) -> bool:
    """A capture element's audio: on unless it says otherwise."""
    for k in AUDIO_KEYS:
        if isinstance(props.get(k), bool):
            return props[k]
    if isinstance(props.get("muted"), bool):
        return not props["muted"]
    return True


AV_KEYS = ("av_offset_ms", "av_sync_ms", "avOffsetMs", "audio_delay_ms", "avSyncMs")
MAX_AV_TRIM = 500


def av_offset(props: dict) -> int:
    """The A/V trim in ms (+ holds the picture, - delays the sound), ±500."""
    for k in AV_KEYS:
        v = props.get(k)
        if v is None or isinstance(v, bool):
            continue
        try:
            return max(-MAX_AV_TRIM, min(MAX_AV_TRIM, int(round(float(v)))))
        except (TypeError, ValueError):
            continue
    return 0


def zone_trigger_id(props: dict) -> Optional[int]:
    t = props.get("triggerId")
    try:
        d = float(t) if not isinstance(t, bool) and t is not None else -1
    except (TypeError, ValueError):
        return None
    return int(d) if 1 <= d <= 9999 and abs(d - round(d)) < 1e-9 else None


def definition_hash(definition: dict) -> str:
    return hashlib.sha1(json.dumps(definition, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def all_types(definition: dict) -> Set[str]:
    return {str(e.get("type") or "") for e in definition.get("elements") or [] if isinstance(e, dict)}


class TemplateBuilder:
    def __init__(self, fetch_def: FetchDef, ensure_media: EnsureMedia):
        self._fetch_def = fetch_def
        self._ensure_media = ensure_media

    async def build(self, tid: str, definition: dict) -> Optional[dict]:
        """The page payload and what it needs, or None when the template uses
        an element type this player does not draw (→ the web render)."""
        self.files: Set[str] = {f"template_{tid}.json"}
        self.rss: Set[str] = set()
        self.media: Dict[str, Path] = {}
        tree = await self._normalise(tid, definition, depth=0, chain=(tid,))
        if tree is None:
            return None
        model = Model.parse(definition.get("interaction"), log=lambda s: log.info(f"[wall] {s}"))
        if model is not None:
            for cid in model.content_ids():
                await self._media_for(cid, "video")
        zones = [e for e in tree["elements"] if e["type"] == "trigger_zone"]
        canonical_keys(zones)
        return {
            "payload": {
                "tid": tid,
                **tree,
                "media": {cid: {"url": f"/media/{p.name}", "kind": "image" if cachemod.is_image_file(p) else "video"}
                          for cid, p in self.media.items()},
                "interaction": {"touch": model.policy.touch, "fade_ms": model.policy.fade_ms,
                                "has_fullscreen": model.has_fullscreen_target} if model else None,
            },
            "files": set(self.files),
            "rss": set(self.rss),
            "media": dict(self.media),
            "model": model,
            "zones": zones,
            "hash": definition_hash(definition),
        }

    async def _media_for(self, cid: Optional[str], role: str) -> Optional[str]:
        if not cid:
            return None
        if cid not in self.media:
            path = await self._ensure_media(cid, role)
            if path is None:
                return None
            self.media[cid] = path
            self.files.add(path.name)
        return cid

    async def _normalise(self, tid: str, definition: dict, depth: int, chain: tuple) -> Optional[dict]:
        out = []
        for e in definition.get("elements") or []:
            if not isinstance(e, dict):
                continue
            etype = str(e.get("type") or "")
            if etype not in KNOWN_TYPES:
                log.info(f"[TEMPLATE] {tid}: element type '{etype}' is not drawn natively — using the web render")
                return None
            props = e.get("props") if isinstance(e.get("props"), dict) else {}
            el = {
                "id": e.get("id"), "type": etype, "label": e.get("label"),
                "x": _num(e.get("x")), "y": _num(e.get("y")),
                "width": max(0.0, _num(e.get("width"))), "height": max(0.0, _num(e.get("height"))),
                "rotation": _num(e.get("rotation")), "zIndex": int(_num(e.get("zIndex"), 1)),
                "props": props,
            }
            if etype in ("image", "video"):
                el["media"] = await self._media_for(media_id(props), etype)
            elif etype == "trigger_zone":
                el["trigger_id"] = zone_trigger_id(props)
                el["media"] = await self._media_for(props.get("attractorContentId"), "video")
            elif etype == "rss":
                cid = media_id(props)
                if cid:
                    self.rss.add(cid)
                el["feed"] = cid
            elif etype == "capture":
                el["device"] = device_label(props)
                el["audio"] = capture_audio(props)
                el["av_offset"] = av_offset(props)
            elif etype == "web":
                el["src"] = str(props.get("src") or props.get("url") or "")
            elif etype == "template":
                el["nested"] = await self._nested(props, depth, chain)
            if etype in ("text", "image", "video", "panel"):
                tap = zone_trigger_id(props)
                if tap is not None:
                    el["tap_trigger"] = tap
            out.append(el)
        # The canvas background, as the CMS render draws it: background_color
        # (black when empty) under background_image_id, cover / centred.
        bg_image = await self._media_for(definition.get("background_image_id"), "image")
        return {
            "width": int(_num(definition.get("width"), 1920)) or 1920,
            "height": int(_num(definition.get("height"), 1080)) or 1080,
            "background": definition.get("background_color"),
            "background_image": bg_image,
            "timezone": definition.get("timezone"),
            "elements": out,
        }

    async def _nested(self, props: dict, depth: int, chain: tuple) -> Optional[dict]:
        child_id = props.get("templateId") or props.get("template_id")
        if not child_id or depth + 1 > MAX_DEPTH:
            return None
        child_id = str(child_id)
        if child_id in chain:
            log.info(f"[TEMPLATE] nested template {child_id} would loop — left empty")
            return None
        child = await self._fetch_def(child_id)
        if not isinstance(child, dict):
            return None
        self.files.add(f"template_{child_id}.json")
        tree = await self._normalise(child_id, child, depth + 1, chain + (child_id,))
        if tree is None:
            return None
        tree.pop("timezone", None)
        tree["fit"] = props.get("fit") if props.get("fit") in ("contain", "cover", "stretch") else "contain"
        tree["show_background"] = props.get("background", True) is not False
        return tree


def nested_placement(fit: str, box_w: float, box_h: float, w: float, h: float) -> Tuple[float, float, float, float]:
    """(scale x, scale y, left, top) of a nested canvas in its box."""
    if w <= 0 or h <= 0:
        return 1.0, 1.0, 0.0, 0.0
    if fit == "stretch":
        return box_w / w, box_h / h, 0.0, 0.0
    s = max(box_w / w, box_h / h) if fit == "cover" else min(box_w / w, box_h / h)
    return s, s, (box_w - w * s) / 2, (box_h - h * s) / 2


def patch_definition(definition: dict, args: dict) -> Tuple[bool, str, Optional[dict]]:
    """Apply a template_patch to a definition copy → (ok, message, new definition).
    The message is the ack wording on refusal; the caller adds media and acks."""
    eid = args.get("element_id")
    if not eid:
        return False, "template_patch needs 'element_id'", None
    d = copy.deepcopy(definition)
    el = next((e for e in d.get("elements") or [] if isinstance(e, dict) and e.get("id") == eid), None)
    if el is None:
        return False, f"element {eid} not found", None
    kind = str(args.get("kind") or args.get("type") or el.get("type") or "")
    if kind not in PATCHABLE:
        return False, f"kind {kind} not patchable", None
    props = el.setdefault("props", {})
    if isinstance(args.get("props"), dict) and (args.get("type") or kind in ("capture", "rss")):
        el["props"] = props = dict(args["props"])
    if args.get("type"):
        el["type"] = args["type"]
    elif kind != el.get("type"):
        el["type"] = kind
    if kind == "text":
        if "text" in args:
            props["content"] = str(args.get("text") or "")
    elif kind in ("image", "video", "rss"):
        cid = args.get("content_id")
        if not cid:
            return False, "template_patch needs 'content_id'", None
        for k in MEDIA_KEYS:
            props.pop(k, None)
        props["contentId"] = str(cid)
    return True, f"patched {eid}", d

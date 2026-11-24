"""
Display surfaces: where the playlist engine and the player draw.

PageSurface  — the renderer page in a Chromium kiosk (the normal path):
               crossfades, preloading, native templates, interactive walls,
               live capture, the player's own screens, all offline.
MpvSurface   — mpv alone, the fallback when Chromium is not installed:
               hard cuts, templates and web pages in a separate browser
               window, screens rendered to PNG.

Both expose the same methods so PlaylistEngine and Player do not care which
one is in use.
"""

import base64
import io
import logging
from pathlib import Path
from typing import Callable, Optional

from . import screens
from .items import IMAGE, TEMPLATE, WEB, Item

log = logging.getLogger("player")


def qr_data_url(link: Optional[str]) -> Optional[str]:
    if not link:
        return None
    img = screens.qr_image(link, 330)
    if img is None:
        return None
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


class PageSurface:
    """Talks to the renderer page through RendererServer."""

    supports_native = True

    def __init__(self, server, *, web_probe: Optional[Callable] = None):
        self.server = server
        self.gen = 0
        self._status: dict = {}
        self._eof_cb: Optional[Callable[[int], None]] = None
        self._page_key: Optional[str] = None
        self._web_probe = web_probe    # async (item) -> dict of extra fields for the web message
        self.template_on_screen: Optional[Item] = None
        self.window = None             # Browser for pages that refuse to be framed

    # -- events from the page ---------------------------------------------

    def set_eof_callback(self, fn: Callable[[int], None]) -> None:
        self._eof_cb = fn

    def on_page_event(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "status":
            self._status = msg
        elif t == "ended" and self._eof_cb:
            self._eof_cb(int(msg.get("gen", -1)))

    @property
    def time_pos(self) -> Optional[float]:
        if self._status.get("gen") != self.gen:
            return None
        return self._status.get("position")

    @property
    def duration(self) -> Optional[float]:
        if self._status.get("gen") != self.gen:
            return None
        return self._status.get("duration")

    def _next(self) -> int:
        self.gen += 1
        return self.gen

    async def _close_window(self) -> None:
        if self.window is not None:
            await self.window.stop()

    # -- playlist content ----------------------------------------------------

    async def show_media(self, item: Item, is_image: bool, loop: bool, crop, preload: Optional[Item] = None) -> int:
        await self._close_window()
        gen = self._next()
        self._page_key = None
        self.template_on_screen = None
        kind = "image" if is_image else ("audio" if item.is_audio else "video")
        msg = {"type": "media", "gen": gen, "url": f"/media/{item.path.name}", "kind": kind,
               "loop": loop, "start": item.clip_start, "end": item.out_point,
               "crop": list(crop) if crop else None, "title": item.name}
        if preload is not None and preload.path is not None:
            msg["preload"] = {"url": f"/media/{preload.path.name}",
                              "kind": "image" if preload.kind == IMAGE else "video",
                              "start": preload.clip_start}
        self.server.send(msg)
        return gen

    def showing_page(self, item: Item) -> bool:
        return self._page_key is not None and self._page_key == f"{item.kind}:{item.url}:{item.signature()}"

    async def show_page(self, item: Item, force: bool = False) -> bool:
        gen = self._next()
        key = f"{item.kind}:{item.url}:{item.signature()}"
        if item.kind == TEMPLATE and item.extra.get("definition") is not None:
            await self._close_window()
            self.server.send({"type": "template", "gen": gen, **item.extra["page"]})
            self.template_on_screen = item
            self._page_key = key
            return True
        self.template_on_screen = None
        extra = {}
        if self._web_probe is not None and item.kind == WEB:
            extra = await self._web_probe(item) or {}
        if extra.get("window"):
            # The site refuses to be framed: its own kiosk window, over the page.
            self.server.send({"type": "screen", "name": "black"})
            ok = await self.window.show(item.url) if self.window is not None else False
            self._page_key = key if ok else None
            return ok
        await self._close_window()
        msg = {"type": "web", "gen": gen, "url": item.url}
        if item.kind == TEMPLATE and item.canvas[0] > 0:
            msg["canvas"] = list(item.canvas)
        msg.update({k: v for k, v in extra.items() if k != "window"})
        self.server.send(msg)
        self._page_key = key
        return True

    async def merge_page(self, item: Item) -> None:
        """An edited template already on screen: the page swaps only what changed."""
        if item.extra.get("definition") is None:
            await self.show_page(item, force=True)
            return
        self.server.send({"type": "template_merge", "gen": self.gen, **item.extra["page"]})
        self.template_on_screen = item
        self._page_key = f"{item.kind}:{item.url}:{item.signature()}"

    async def hide_page(self) -> None:
        await self._close_window()
        self._page_key = None
        self.template_on_screen = None

    def pause(self, paused: bool) -> None:
        self.server.send({"type": "pause", "paused": bool(paused)})

    def seek(self, seconds: float) -> None:
        self.server.send({"type": "seek", "to": seconds})

    def set_crop(self, crop) -> None:
        self.server.send({"type": "crop", "crop": list(crop) if crop else None})

    async def stop(self) -> None:
        await self.hide_page()
        self._next()
        self.server.send({"type": "stop"})

    async def release(self) -> None:
        await self.stop()

    # -- the player's own screens and notices ---------------------------------

    def show_screen(self, name: str, **fields) -> None:
        self._page_key = None
        self.template_on_screen = None
        self._next()
        if name == "enrollment":
            fields["qr"] = qr_data_url(fields.pop("link", None))
        self.server.send({"type": "screen", "name": name, **fields})

    def osd(self, text: str, ms: int = 0) -> None:
        self.server.send({"type": "osd", "text": text, "ms": ms})

    def clear_osd(self) -> None:
        self.server.send({"type": "osd", "text": ""})

    def overlay(self, kind: Optional[str], title: str = "", body: str = "", opaque: bool = True) -> None:
        self.server.send({"type": "overlay", "kind": kind, "title": title, "body": body, "opaque": opaque})

    def set_volume(self, volume: int, muted: bool) -> None:
        self.server.send({"type": "volume", "volume": volume, "muted": muted})

    def download_progress(self, done: int, total: int, fraction: float = 0.0) -> None:
        self.server.send({"type": "download", "done": done, "total": total, "fraction": fraction})

    def send(self, msg: dict) -> None:
        self.server.send(msg)


class MpvSurface:
    """mpv for media, a separate Chromium window for pages, PNG screens."""

    supports_native = False

    def __init__(self, mpv, browser, *, screens_dir: Optional[Path] = None,
                 crop_filter: Callable = lambda c: None):
        self.mpv = mpv
        self.browser = browser
        self.screens_dir = screens_dir
        self._crop_filter = crop_filter
        self.template_on_screen = None
        self._showing_media = False

    def set_eof_callback(self, fn) -> None:
        self.mpv.set_eof_callback(fn)

    @property
    def time_pos(self):
        return self.mpv.time_pos

    @property
    def duration(self):
        return self.mpv.duration

    async def show_media(self, item: Item, is_image: bool, loop: bool, crop, preload=None) -> int:
        await self.browser.stop()
        self._showing_media = True
        vf = self._crop_filter(crop)
        if is_image:
            return self.mpv.play_file(str(item.path), vf=vf)
        return self.mpv.play_file(str(item.path), loop=loop, vf=vf,
                                  start=item.clip_start, end=item.out_point)

    def showing_page(self, item: Item) -> bool:
        return self.browser.showing(item.url)

    async def show_page(self, item: Item, force: bool = False) -> bool:
        self._showing_media = False
        self._black()
        scale = item.extra.get("scale")
        return await self.browser.show(item.url, scale)

    async def hide_page(self) -> None:
        await self.browser.stop()

    async def merge_page(self, item: Item) -> None:
        await self.show_page(item, force=True)

    def pause(self, paused: bool) -> None:
        self.mpv.pause(paused)

    def seek(self, seconds: float) -> None:
        self.mpv.seek(seconds)

    def set_crop(self, crop) -> None:
        if self._showing_media:
            self.mpv.set_filter(self._crop_filter(crop))

    async def stop(self) -> None:
        await self.browser.stop()
        self.mpv.stop()
        self._showing_media = False
        self._black()

    async def release(self) -> None:
        await self.browser.stop()
        self.mpv.stop()
        self._showing_media = False

    # -- screens -------------------------------------------------------------

    def _still(self, name: str, render) -> None:
        if self.screens_dir is None:
            return
        path = self.screens_dir / f"{name}.png"
        try:
            ok = render(path)
        except Exception as e:
            log.warning(f"[SCREEN] {name} failed: {e}")
            ok = False
        if ok:
            self.mpv.clear_osd()
            self.mpv.show_still(str(path))

    def _black(self) -> None:
        self._still("black", screens.render_black)

    def show_screen(self, name: str, **fields) -> None:
        self._showing_media = False
        size = self.mpv.display_size()
        if name == "black":
            self._black()
        elif name == "enrollment":
            if screens.available():
                self._still("enrollment", lambda p: screens.render_enrollment(
                    p, size, fields.get("code", ""), fields.get("server", ""),
                    fields.get("site"), fields.get("link")))
            else:
                self._black()
                self.mpv.show_osd(f"REGISTRATION CODE\n\n  {fields.get('code')}  \n\n"
                                  "Enter this code in CM:EX to approve this player.", 3_600_000)
        elif name == "setup":
            self.show_screen("message", title="cms_url is not configured",
                             body="Set cms_url in config.yaml and restart the player.")
        else:
            title, body = fields.get("title", ""), fields.get("body", "")
            if screens.available():
                self._still(name if name != "message" else f"msg-{abs(hash(title)) % 10000}",
                            lambda p: screens.render_message(p, size, title, body))
            else:
                self._black()
                self.mpv.show_osd(f"{title}\n\n{body}".strip(), 3_600_000)

    def osd(self, text: str, ms: int = 0) -> None:
        self.mpv.show_osd(text, ms or 10_000)

    def clear_osd(self) -> None:
        self.mpv.clear_osd()

    def overlay(self, kind: Optional[str], title: str = "", body: str = "", opaque: bool = True) -> None:
        if not kind:
            self.mpv.clear_osd()
        elif opaque:
            self.show_screen("message", title=title, body=body)
        else:
            self.mpv.show_osd(f"{title}\n{body}", 6000)

    def set_volume(self, volume: int, muted: bool) -> None:
        self.mpv.set_volume(volume, muted)

    def download_progress(self, done: int, total: int, fraction: float = 0.0) -> None:
        pass

    def send(self, msg: dict) -> None:
        pass

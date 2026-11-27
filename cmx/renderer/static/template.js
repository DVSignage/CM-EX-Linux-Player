/*
 * Native templates: a CMS template definition drawn from local files, so it
 * works offline. Mirrors DVSI Plus TemplateRenderer / RssView / NestedTemplates.
 */
"use strict";

CMX.template = (() => {
  const root = document.getElementById("template");
  let cur = null;   // { payload, canvas, els: Map(id -> {def, node}), videos: Set, rss: [], captures: [] }

  const FIT = { contain: "contain", cover: "cover", fill: "fill", stretch: "fill", none: "none", "scale-down": "scale-down" };
  const ALIGN = { left: "left", center: "center", centre: "center", right: "right", justify: "justify" };
  const num = (v, d) => (v === null || v === undefined || v === "" || isNaN(+v)) ? d : +v;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  function mediaUrl(payload, cid) { return cid && payload.media[cid] ? payload.media[cid].url : null; }

  function place(node, e) {
    Object.assign(node.style, { left: `${e.x}px`, top: `${e.y}px`, width: `${e.width}px`, height: `${e.height}px`,
      zIndex: String(e.zIndex || 1) });
    if (Math.abs(e.rotation || 0) > 0.001) { node.style.transform = `rotate(${e.rotation}deg)`; node.style.transformOrigin = "50% 50%"; }
  }

  // ------------------------------------------------------------------ elements
  // Text as the CMS render draws it: a flex box (vertical alignment) with 4px 8px padding
  // and a span the full width; line height is a multiplier of the font size.
  const VALIGN = { top: "flex-start", middle: "center", center: "center", bottom: "flex-end" };
  function buildText(e) {
    const p = e.props, node = document.createElement("div");
    node.className = "el text";
    const inner = document.createElement("div");
    const fs = clamp(num(p.fontSize, 24), 8, 400);
    Object.assign(node.style, {
      background: p.backgroundColor || "transparent", padding: "4px 8px",
      alignItems: VALIGN[(p.verticalAlign || "").toLowerCase()] || "center",
      fontSize: `${fs}px`, color: p.color || "#fff", textAlign: ALIGN[(p.align || "").toLowerCase()] || "left",
      fontFamily: p.fontFamily || "Arial, sans-serif", fontWeight: p.fontWeight || "normal",
      lineHeight: num(p.lineHeight, 0) > 0 ? String(num(p.lineHeight, 0)) : "normal",
    });
    inner.textContent = p.content || "";
    node.appendChild(inner);
    return node;
  }

  // An embedded page (the CMS's `web` element): a borderless iframe, see-through when asked.
  function buildWeb(e) {
    const node = document.createElement("div");
    node.className = "el web";
    if (!e.src) return node;
    const f = document.createElement("iframe");
    f.src = e.src;
    f.setAttribute("allow", e.props.allow || "autoplay; fullscreen");
    if (e.props.transparent) f.setAttribute("allowtransparency", "true");
    f.style.cssText = `position:absolute;inset:0;width:100%;height:100%;border:none;background:${e.props.transparent ? "transparent" : "#fff"}`;
    node.appendChild(f);
    return node;
  }

  function buildPanel(e) {
    const p = e.props, node = document.createElement("div");
    node.className = "el panel";
    Object.assign(node.style, { background: p.backgroundColor || "transparent", borderRadius: `${num(p.borderRadius, 0)}px`,
      border: `${num(p.borderWidth, 0)}px solid ${p.borderColor || "transparent"}`, opacity: String(clamp(num(p.opacity, 1), 0, 1)) });
    return node;
  }

  function mediaSurface(payload, e, url, kind) {
    const p = e.props;
    let s;
    if (!url) {
      // the CMS render's placeholder for an element with no media
      s = document.createElement("div"); s.className = "surf";
      s.style.cssText = "display:flex;align-items:center;justify-content:center;background:#333;color:#999;font-size:14px";
      s.textContent = e.type === "video" ? "No video" : "No image";
      return s;
    }
    if (kind === "image" || e.type === "image") {
      s = document.createElement("img"); s.src = url;
    } else {
      s = document.createElement("video");
      s.src = url; s.autoplay = true; s.playsInline = true;
      s.loop = p.loop !== false;
      s.dataset.ownMuted = p.muted === true ? "1" : "";
      CMX.applyVolume(s, p.muted === true);
      s.addEventListener("canplay", () => s.play().catch(() => {}), { once: true });
      if (cur) cur.videos.add(s);
    }
    s.style.objectFit = FIT[(p.objectFit || "cover").toLowerCase()] || "cover";
    s.style.opacity = String(clamp(num(p.opacity, 1), 0, 1));
    return s;
  }

  function buildMedia(payload, e) {
    const node = document.createElement("div");
    node.className = "el media";
    node.style.background = e.type === "video" ? "#000" : "transparent";
    const kind = e.media && payload.media[e.media] ? payload.media[e.media].kind : e.type;
    node.appendChild(mediaSurface(payload, e, mediaUrl(payload, e.media), kind));
    return node;
  }

  function buildCapture(e) {
    const node = document.createElement("div");
    node.className = "el capture";
    node.style.background = "#000";
    const v = document.createElement("video");
    v.autoplay = true; v.playsInline = true;
    v.style.objectFit = FIT[(e.props.objectFit || "contain").toLowerCase()] || "contain";
    const hint = document.createElement("div");
    hint.className = "hint";
    node.append(v, hint);
    const slot = CMX.capture ? CMX.capture.attach(v, hint, e.device, e.audio, null, e.av_offset || 0) : null;
    if (slot) { node.cmxCapture = slot; if (cur) cur.captures.push(slot); }
    return node;
  }

  // A tile taken off the canvas (an edit swapped or removed it) lets go of its
  // camera; otherwise the old stream kept running, unseen, next to the new one.
  function release(node) {
    for (const el of [node, ...node.querySelectorAll(".el.capture")]) {
      const slot = el.cmxCapture;
      if (!slot) continue;
      slot.stop();
      el.cmxCapture = null;
      if (cur) cur.captures = cur.captures.filter((s) => s !== slot);
    }
  }

  function buildNested(payload, e) {
    const node = document.createElement("div");
    node.className = "el nested";
    const n = e.nested;
    if (!n) return node;
    const inner = document.createElement("div");
    inner.className = "canvas";
    Object.assign(inner.style, { width: `${n.width}px`, height: `${n.height}px`, position: "absolute",
      background: n.show_background ? (n.background || "#000") : "transparent" });
    const bw = e.width, bh = e.height;
    let sx, sy, left = 0, top = 0;
    if (n.fit === "stretch") { sx = bw / n.width; sy = bh / n.height; }
    else {
      const s = n.fit === "cover" ? Math.max(bw / n.width, bh / n.height) : Math.min(bw / n.width, bh / n.height);
      sx = sy = s; left = (bw - n.width * s) / 2; top = (bh - n.height * s) / 2;
    }
    inner.style.transformOrigin = "0 0";
    inner.style.transform = `translate(${left}px, ${top}px) scale(${sx}, ${sy})`;
    for (const c of n.elements) { const child = build(payload, c, true); if (child) inner.appendChild(child); }
    node.appendChild(inner);
    return node;
  }

  function build(payload, e, nested) {
    let node;
    switch (e.type) {
      case "text": node = buildText(e); break;
      case "panel": node = buildPanel(e); break;
      case "image": case "video": node = buildMedia(payload, e); break;
      case "capture": node = buildCapture(e); break;
      case "rss": node = buildRss(e); break;
      case "trigger_zone": node = nested ? document.createElement("div") : CMX.wall.buildZone(payload, e); break;
      case "template": node = buildNested(payload, e); break;
      case "web": node = buildWeb(e); break;
      default: return null;
    }
    node.classList.add("el");
    place(node, e);
    if (!nested && e.id) { node.dataset.eid = e.id; if (cur) cur.els.set(e.id, { def: e, node }); }
    if (!nested && e.tap_trigger) node.dataset.tap = String(e.tap_trigger);
    return node;
  }

  // ------------------------------------------------------------------ RSS
  // backgroundOpacity (0-100) applied to a #rgb / #rrggbb colour; other colours as they are
  function withOpacity(color, opacity) {
    if (!color) return "transparent";
    const o = num(opacity, 100);
    const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color);
    if (!m || o >= 100) return color;
    let h = m[1]; if (h.length === 3) h = h.split("").map((c) => c + c).join("");
    return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${clamp(o, 0, 100) / 100})`;
  }
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  function friendlyDate(s, fmt) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || "");
    if (!m) return "";
    const y = +m[1], mo = +m[2], d = +m[3];
    if (fmt === "yyyy-MM-dd") return `${m[1]}-${m[2]}-${m[3]}`;
    return `${MONTHS[mo - 1]} ${d}, ${y}`;   // the item's own date, never shifted to local time
  }

  function buildRss(e) {
    const p = e.props, node = document.createElement("div");
    node.className = "el rss";
    const fs = clamp(num(p.fontSize, 28), 8, 400);
    Object.assign(node.style, { background: withOpacity(p.backgroundColor, p.backgroundOpacity), color: p.color || "#fff", fontSize: `${fs}px`,
      fontFamily: p.fontFamily || "", fontWeight: p.fontWeight || "normal", textAlign: ALIGN[(p.align || "").toLowerCase()] || "left" });
    const mode = (p.mode || "ticker").toLowerCase() === "list" ? "list" : "ticker";
    const maxItems = Math.max(1, num(p.maxItems, mode === "list" ? 8 : 25));
    const itemS = Math.max(2, num(p.itemDurationS, 8));
    const speed = Math.max(10, num(p.speedPxS, num(p.speed, 90)));
    const sep = p.separator != null && p.separator !== "" ? String(p.separator) : " • ";
    const showImages = p.showImages === true;
    const reverse = (p.animation || "") === "scroll-reverse";
    const showTs = p.showTimestamps !== false;
    const showSummary = p.showSummary !== false && p.showDescription !== false;
    let lastKey = "", page = 0, pageTimer = null, items = [];

    function renderTicker() {
      node.innerHTML = "";
      node.style.display = "flex"; node.style.alignItems = "center";
      node.style.maskImage = node.style.webkitMaskImage = "linear-gradient(to right, transparent 0, #000 3%, #000 97%, transparent 100%)";
      const track = document.createElement("div");
      track.style.whiteSpace = "nowrap"; track.style.display = "inline-block"; track.style.willChange = "transform";
      // a separator after EVERY item, as the CMS markup, so copies never collide
      const one = items.slice(0, maxItems).map((it) => `<span>${CMX.esc(it.title)}</span><span style="opacity:.55;padding:0 .6em">${CMX.esc(sep)}</span>`).join("");
      track.innerHTML = `<span class="copy">${one}</span>`;
      node.appendChild(track);
      const copy = track.querySelector(".copy");
      if (!one) return;
      while (copy.offsetWidth < node.clientWidth * 1.2 && copy.offsetWidth > 0) copy.innerHTML += one;
      track.innerHTML += `<span class="copy">${copy.innerHTML}</span>`;
      const w = copy.offsetWidth;
      track.animate([{ transform: "translateX(0)" }, { transform: `translateX(${-w}px)` }],
        { duration: Math.max(1000, (w / speed) * 1000), iterations: Infinity, easing: "linear",
          direction: reverse ? "reverse" : "normal" });
    }

    function renderListPage() {
      node.innerHTML = "";
      node.style.display = "block";
      const inset = Math.min(fs * 0.45, e.width * 0.06);
      const pages = Math.max(1, Math.ceil(items.length / maxItems));
      const slice = items.slice((page % pages) * maxItems, (page % pages) * maxItems + maxItems);
      const rowH = e.height / Math.max(1, Math.min(maxItems, slice.length || 1));
      slice.forEach((it, i) => {
        const row = document.createElement("div");
        // the CMS render's row: title / summary / date, a hairline between rows
        Object.assign(row.style, { display: "flex", flexDirection: "column", gap: ".15em", padding: `.45em ${inset}px`,
          height: `${rowH}px`, overflow: "hidden", borderBottom: i < slice.length - 1 ? "1px solid rgba(255,255,255,.12)" : "0",
          opacity: "0", transform: "translateY(14px)", transition: "opacity 450ms ease, transform 450ms ease" });
        const lines = rowH >= 4.2 * fs ? 2 : (rowH >= 3.2 * fs ? 1 : 0);
        row.innerHTML = (showImages && it.image_url ? `<img src="${CMX.esc(it.image_url)}" alt="" style="width:100%;max-height:40%;object-fit:cover;border-radius:4px;margin-bottom:.25em">` : "") +
          `<div style="font-weight:600;line-height:1.25">${CMX.esc(it.title)}</div>` +
          (showSummary && lines && it.summary ? `<div style="font-size:.8em;opacity:.75;line-height:1.3;overflow:hidden;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:${lines}">${CMX.esc(it.summary)}</div>` : "") +
          (showTs && it.published ? `<div style="font-size:.7em;opacity:.55">${CMX.esc(friendlyDate(it.published, p.dateFormat))}</div>` : "");
        node.appendChild(row);
        setTimeout(() => { row.style.opacity = "1"; row.style.transform = "none"; }, 30 + i * 70);
      });
      if (pages > 1) { clearTimeout(pageTimer); pageTimer = setTimeout(() => { page++; renderListPage(); }, itemS * 1000); }
    }

    async function poll() {
      if (!e.feed) return;
      try {
        const r = await fetch(`/api/rss/${encodeURIComponent(e.feed)}`, { cache: "no-store" });
        const data = await r.json();
        const next = (data && data.items) || [];
        const key = JSON.stringify(next);
        if (!next.length || key === lastKey) return;   // an unchanged payload never restarts the ticker
        lastKey = key; items = next; page = 0;
        if (mode === "ticker") renderTicker(); else renderListPage();
      } catch (err) { /* keep what is shown */ }
    }
    poll();
    const timer = setInterval(poll, 30000);
    if (cur) cur.rss.push(() => { clearInterval(timer); clearTimeout(pageTimer); });
    return node;
  }

  // ------------------------------------------------------------------ lifecycle
  function layout() {
    if (!cur) return;
    const p = cur.payload, sw = window.innerWidth, sh = window.innerHeight;
    const s = Math.min(sw / p.width, sh / p.height);
    cur.scale = s;
    Object.assign(cur.canvas.style, { width: `${p.width}px`, height: `${p.height}px`,
      transform: `translate(${(sw - p.width * s) / 2}px, ${(sh - p.height * s) / 2}px) scale(${s})` });
  }
  window.addEventListener("resize", layout);

  function teardown() {
    if (!cur) return;
    if (CMX.wall) CMX.wall.detach();
    for (const v of cur.videos) { try { v.pause(); v.removeAttribute("src"); v.load(); } catch (e) {} }
    for (const stop of cur.rss) stop();
    for (const c of cur.captures) c.stop();
    root.innerHTML = "";
    cur = null;
  }

  function show(m) {
    teardown();
    CMX.state.gen = m.gen; CMX.state.kind = "template";
    CMX.showSurface("template");
    cur = { payload: m, canvas: document.createElement("div"), els: new Map(), videos: new Set(), rss: [], captures: [], scale: 1 };
    cur.canvas.className = "canvas";
    // as the CMS render: background_color (black when empty) under the background image
    cur.canvas.style.backgroundColor = m.background || "#000";
    const bg = mediaUrl(m, m.background_image);
    if (bg) Object.assign(cur.canvas.style, { backgroundImage: `url("${bg}")`, backgroundSize: "cover",
      backgroundPosition: "center", backgroundRepeat: "no-repeat" });
    root.appendChild(cur.canvas);
    layout();
    for (const e of m.elements) { const node = build(m, e, false); if (node) cur.canvas.appendChild(node); }
    if (m.interaction && CMX.wall) CMX.wall.attach(cur, m);
    CMX.post("template_shown", { gen: m.gen, tid: m.tid });
  }

  // Swap one element for a new build, crossfaded: a live patch or an edit.
  function swap(id, e, payload) {
    const old = cur.els.get(id);
    const node = build(payload, e, false);
    if (!node) return false;
    node.style.opacity = "0";
    node.style.transition = "opacity 400ms ease";
    if (old) old.node.after(node); else cur.canvas.appendChild(node);
    const v = node.querySelector("video");
    const reveal = () => {
      node.style.opacity = "1";
      if (old) setTimeout(() => {
        for (const ov of old.node.querySelectorAll("video")) { ov.pause(); cur && cur.videos.delete(ov); }
        release(old.node);
        old.node.remove();
      }, 450);
    };
    if (v) { let done = false; const go = () => { if (!done) { done = true; reveal(); } }; v.addEventListener("playing", go, { once: true }); setTimeout(go, 4000); }
    else requestAnimationFrame(reveal);
    return true;
  }

  function patch(m) {
    if (!cur || cur.payload.tid !== m.tid) return;
    Object.assign(cur.payload.media, m.media || {});
    swap(m.element.id, m.element, cur.payload);
  }

  // An edited template, same entry in the playlist: refresh only what changed.
  function merge(m) {
    if (!cur || cur.payload.tid !== m.tid || cur.payload.width !== m.width || cur.payload.height !== m.height
        || JSON.stringify(cur.payload.interaction) !== JSON.stringify(m.interaction)) { show(m); return; }
    const oldDefs = new Map(cur.payload.elements.map((e) => [e.id, JSON.stringify(e)]));
    cur.payload = Object.assign(m, { media: Object.assign({}, cur.payload.media, m.media) });
    const keep = new Set();
    for (const e of m.elements) {
      keep.add(e.id);
      if (!e.id) continue;
      if (oldDefs.get(e.id) !== JSON.stringify(e)) swap(e.id, e, m);
    }
    for (const [id, rec] of cur.els) {
      if (!keep.has(id)) { rec.node.style.transition = "opacity 400ms ease"; rec.node.style.opacity = "0"; release(rec.node); setTimeout(() => rec.node.remove(), 450); cur.els.delete(id); }
    }
    CMX.state.gen = m.gen;
  }

  CMX.on("template", show);
  CMX.on("template_patch", patch);
  CMX.on("template_merge", merge);

  return {
    clear: teardown,
    get current() { return cur; },
    setPaused(p) { if (!cur) return; for (const v of cur.videos) { if (p) v.pause(); else v.play().catch(() => {}); } },
    volume() { if (!cur) return; for (const v of cur.videos) CMX.applyVolume(v, v.dataset.ownMuted === "1"); },
    restart() { if (!cur) return; for (const v of cur.videos) { try { v.currentTime = 0; v.play().catch(() => {}); } catch (e) {} } },
    layout,
  };
})();

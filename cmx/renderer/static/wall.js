/*
 * Interactive walls on the page: trigger zones, the fullscreen surface, scene
 * attractors and layouts, the alignment overlay and touch taps. The rules
 * (what a trigger does) live in the player (cmx/interaction.py); this file
 * only draws what it is told and reports when surfaces play and return.
 */
"use strict";

CMX.wall = (() => {
  let ctx = null;        // { tpl (template state), payload, surfaces: Map(key -> surface), attractor, overlay }
  const ZONE_FIT = { fit: "contain", stretch: "fill" };       // anything else: cover
  const FS_FIT = { fill: "cover", fit: "contain", stretch: "fill" };

  function zoneKeyFor(e) {
    // The player names a lone zone by its trigger, a shared or unwired one by element id.
    return e.surface_key || (e.trigger_id ? `zone:${e.trigger_id}` : `el:${e.id}`);
  }

  // One playable surface: an idle layer (the zone's attractor loop) under a
  // play layer that fades in for content and back out when it ends.
  function makeSurface(node, idleUrl, fit) {
    const idle = document.createElement("div");
    idle.className = "surf";
    if (idleUrl) {
      const v = document.createElement("video");
      Object.assign(v, { src: idleUrl, autoplay: true, loop: true, muted: true, playsInline: true });
      v.style.cssText = `width:100%;height:100%;object-fit:${fit}`;
      idle.appendChild(v);
    }
    node.appendChild(idle);
    return { node, idle, fit, layer: null, busy: false, timer: null, key: null };
  }

  function buildZone(payload, e) {
    const node = document.createElement("div");
    node.className = "el zone";
    const fit = ZONE_FIT[(e.props.fit || "").toLowerCase()] || "cover";
    const idleUrl = e.media && payload.media[e.media] ? payload.media[e.media].url : null;
    const s = makeSurface(node, idleUrl, fit);
    node._surface = s;
    if (e.trigger_id) node.dataset.tap = String(e.trigger_id);
    return node;
  }

  function attach(tpl, payload) {
    ctx = { tpl, payload, surfaces: new Map(), attractor: null, overlay: null, fullscreen: null };
    for (const [id, rec] of tpl.els) {
      if (rec.def.type !== "trigger_zone" || !rec.node._surface) continue;
      const key = zoneKeyFor(rec.def);
      rec.node._surface.key = key;
      if (!ctx.surfaces.has(key)) ctx.surfaces.set(key, []);
      ctx.surfaces.get(key).push(rec.node._surface);
      // a zone named by element id is also reachable that way
      const ek = `el:${id}`;
      if (ek !== key) { if (!ctx.surfaces.has(ek)) ctx.surfaces.set(ek, []); ctx.surfaces.get(ek).push(rec.node._surface); }
    }
    if (payload.interaction && payload.interaction.touch) {
      tpl.canvas.addEventListener("pointerdown", onTap);
    }
  }

  function onTap(ev) {
    let n = ev.target;
    while (n && n !== ctx.tpl.canvas && !n.dataset.tap) n = n.parentElement;
    if (n && n.dataset && n.dataset.tap) CMX.post("tap", { trigger_id: +n.dataset.tap });
  }

  function fullscreen() {
    if (!ctx.fullscreen) {
      const node = document.createElement("div");
      node.className = "el";
      Object.assign(node.style, { left: "0", top: "0", width: `${ctx.payload.width}px`, height: `${ctx.payload.height}px`,
        zIndex: "5000", pointerEvents: "none" });
      ctx.tpl.canvas.appendChild(node);
      ctx.fullscreen = makeSurface(node, null, "contain");
      ctx.fullscreen.key = "fullscreen";
    }
    return [ctx.fullscreen];
  }

  function surfacesFor(key) { return key === "fullscreen" ? fullscreen() : (ctx.surfaces.get(key) || []); }

  function endSurface(s, report) {
    clearTimeout(s.timer);
    const layer = s.layer;
    s.layer = null;
    if (layer) {
      layer.style.opacity = "0";
      setTimeout(() => { for (const v of layer.querySelectorAll("video")) v.pause(); layer.remove(); }, (ctx ? ctx.fade : 300) + 50);
    }
    if (s.busy) { s.busy = false; if (report) CMX.post("zone_returned", { surface: s.key }); }
  }

  function play(m) {
    if (!ctx) return;
    ctx.fade = m.fade_ms || 0;
    const list = surfacesFor(m.surface);
    list.forEach((s, i) => {
      const old = s.layer;
      clearTimeout(s.timer);
      const layer = document.createElement("div");
      layer.className = "surf";
      layer.style.opacity = "0";
      layer.style.transition = `opacity ${ctx.fade}ms ease`;
      const fit = m.surface === "fullscreen" ? (FS_FIT[m.fit] || "contain") : s.fit;
      let el;
      if (m.kind === "image") { el = document.createElement("img"); el.src = m.url; }
      else {
        el = document.createElement("video");
        Object.assign(el, { src: m.url, autoplay: true, playsInline: true });
        CMX.applyVolume(el, false);
        el.onended = () => { if (s.layer === layer) endSurface(s, true); };
      }
      el.style.cssText = `width:100%;height:100%;object-fit:${fit};background:${m.surface === "fullscreen" ? "#000" : "transparent"}`;
      layer.appendChild(el);
      s.node.appendChild(layer);
      s.layer = layer; s.busy = true;
      const shown = () => {
        if (s.layer !== layer) return;
        layer.style.opacity = "1";
        if (old) setTimeout(() => { for (const v of old.querySelectorAll("video")) v.pause(); old.remove(); }, ctx.fade + 50);
        if (i === 0) CMX.post("zone_playing", { surface: m.surface });
        if (m.kind === "image") s.timer = setTimeout(() => endSurface(s, true), (m.hold_s || 10) * 1000);
      };
      if (m.kind === "image") { if (el.complete) shown(); else el.onload = shown; }
      else el.addEventListener("playing", shown, { once: true });
    });
    if (!list.length) CMX.post("zone_returned", { surface: m.surface });
  }

  function restart(m) {
    for (const s of surfacesFor(m.surface)) {
      if (!s.layer) continue;
      const v = s.layer.querySelector("video");
      if (v) { try { v.currentTime = 0; v.play().catch(() => {}); } catch (e) {} }
      else { clearTimeout(s.timer); s.timer = setTimeout(() => endSurface(s, true), (m.hold_s || 10) * 1000); }
    }
  }

  function returnAll() {
    if (!ctx) return;
    const seen = new Set();
    for (const list of ctx.surfaces.values()) for (const s of list) { if (!seen.has(s)) { seen.add(s); endSurface(s, true); } }
    if (ctx.fullscreen) endSurface(ctx.fullscreen, true);
  }

  function attractor(m) {
    if (!ctx) return;
    const fade = m.fade_ms || 0;
    const old = ctx.attractor;
    ctx.attractor = null;
    if (old) { old.style.transition = `opacity ${fade}ms`; old.style.opacity = "0"; setTimeout(() => old.remove(), fade + 50); }
    if (m.cid === null || m.cid === undefined) return;          // the template's own elements
    const node = document.createElement("div");
    node.className = "el";
    Object.assign(node.style, { left: "0", top: "0", width: `${ctx.payload.width}px`, height: `${ctx.payload.height}px`,
      zIndex: "900", background: "#000", opacity: "0", transition: `opacity ${fade}ms` });
    if (m.url) {
      const fit = FS_FIT[m.fit] || "cover";
      let el;
      if (m.kind === "image") { el = document.createElement("img"); el.src = m.url; }
      else { el = document.createElement("video"); Object.assign(el, { src: m.url, autoplay: true, loop: true, muted: true, playsInline: true }); }
      el.style.cssText = `width:100%;height:100%;object-fit:${fit}`;
      node.appendChild(el);
    }
    ctx.tpl.canvas.appendChild(node);
    // zones stay above the attractor
    for (const list of ctx.surfaces.values()) for (const s of list) s.node.style.zIndex = String(Math.max(1000, +s.node.style.zIndex || 0));
    requestAnimationFrame(() => { node.style.opacity = "1"; });
    ctx.attractor = node;
  }

  function sceneLayout(m) {
    if (!ctx) return;
    const fade = m.fade_ms || 0, over = m.overrides || {};
    for (const [id, rec] of ctx.tpl.els) {
      const e = rec.def, o = over[id] || {};
      const n = rec.node;
      n.style.transition = fade ? `left ${fade}ms, top ${fade}ms, width ${fade}ms, height ${fade}ms, opacity ${fade}ms` : "";
      Object.assign(n.style, {
        left: `${o.x ?? e.x}px`, top: `${o.y ?? e.y}px`, width: `${o.width ?? e.width}px`, height: `${o.height ?? e.height}px`,
        opacity: o.visible === false ? "0" : "", visibility: "",
      });
      if (o.visible === false) setTimeout(() => { if ((ctx && (ctx.lastLayout || {})[id] || {}).visible === false) n.style.visibility = "hidden"; }, fade);
      if (e.type === "text") { const t = n.firstElementChild; if (t) t.textContent = o.content ?? (e.props.content || ""); }
      if (e.type === "image") {
        const img = n.querySelector("img");
        const url = o.contentUrl || (e.media && ctx.payload.media[e.media] ? ctx.payload.media[e.media].url : null);
        if (img && url && img.getAttribute("src") !== url) img.src = url;
      }
    }
    ctx.lastLayout = over;
    if (ctx.alignment) alignment({ on: true, scene: ctx.alignmentScene });
  }

  function alignment(m) {
    if (!ctx) return;
    if (ctx.overlay) { ctx.overlay.remove(); ctx.overlay = null; }
    ctx.alignment = !!m.on; ctx.alignmentScene = m.scene;
    if (!m.on) return;
    const ov = document.createElement("div");
    ov.className = "el";
    Object.assign(ov.style, { left: "0", top: "0", width: `${ctx.payload.width}px`, height: `${ctx.payload.height}px`, zIndex: "9000", pointerEvents: "none" });
    for (const [id, rec] of ctx.tpl.els) {
      const e = rec.def;
      const isZone = e.type === "trigger_zone";
      if (!isZone && !e.tap_trigger) continue;
      const o = (ctx.lastLayout || {})[id] || {};
      const b = document.createElement("div");
      b.className = "align-box" + (isZone ? "" : " tap") + (o.visible === false ? " hidden" : "");
      Object.assign(b.style, { left: `${o.x ?? e.x}px`, top: `${o.y ?? e.y}px`, width: `${o.width ?? e.width}px`, height: `${o.height ?? e.height}px` });
      const tid = isZone ? e.trigger_id : e.tap_trigger;
      b.innerHTML = `<span>${CMX.esc(e.label || id)}${tid ? ` / Trigger ${tid}` : ""}${o.visible === false ? " (hidden here)" : ""}</span>`;
      ov.appendChild(b);
    }
    const cap = document.createElement("div");
    cap.className = "align-box"; Object.assign(cap.style, { left: "16px", top: "16px", border: "0" });
    cap.innerHTML = `<span>Scene: ${CMX.esc(m.scene || "")}</span>`;
    ov.appendChild(cap);
    ctx.tpl.canvas.appendChild(ov);
    ctx.overlay = ov;
  }

  CMX.on("wall", (m) => {
    switch (m.op) {
      case "play": return play(m);
      case "restart": return restart(m);
      case "return_all": return returnAll();
      case "attractor": return attractor(m);
      case "layout": return sceneLayout(m);
      case "alignment": return alignment(m);
      case "reset": returnAll(); if (CMX.template) CMX.template.restart(); return;
    }
  });

  return {
    buildZone, attach,
    detach() { if (ctx && ctx.tpl && ctx.tpl.canvas) ctx.tpl.canvas.removeEventListener("pointerdown", onTap); ctx = null; },
  };
})();

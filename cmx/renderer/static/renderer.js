/*
 * CM:EX Linux Player — renderer page (core).
 *
 * The player (Python) decides WHAT is on screen and pushes it here over
 * Server-Sent Events (/events); this page decides HOW: crossfades, preloading,
 * clip in/out, wall crop, the Now Playing card and the player's own screens.
 * It reports back on /api/event: ended, status (position), errors, liveness.
 *
 * template.js, wall.js and capture.js register more message handlers on CMX.
 */
"use strict";

const CMX = (() => {
  const handlers = {};
  const state = {
    gen: -1,              // generation of what is on screen (from the player)
    kind: null,           // "video" | "image" | "audio" | "web" | "template" | "capture" | "screen"
    volume: 100,
    muted: false,
    config: { loading_indicator: false, music_wave: true },
    fadeMs: 400,
  };
  const $ = (id) => document.getElementById(id);

  function on(type, fn) { handlers[type] = fn; }

  function post(type, data) {
    const body = JSON.stringify(Object.assign({ type }, data || {}));
    return fetch("/api/event", { method: "POST", headers: { "Content-Type": "application/json" }, body })
      .catch(() => {});
  }

  // Which full-screen surface is visible. Everything else is hidden (and its
  // media paused by its owner) so nothing plays underneath.
  const SURFACES = ["media", "web", "snapshot", "template", "capture", "screen"];
  function showSurface(name) {
    for (const s of SURFACES) {
      const el = $(s);
      if (s === "screen") el.classList.toggle("on", name === "screen");
      else el.style.display = (s === name) ? "block" : "none";
    }
    if (name !== "media") music.hide();
    if (name !== "web") { const f = $("web"); if (f.src && f.src !== "about:blank") f.src = "about:blank"; }
    if (name !== "template" && CMX.template) CMX.template.clear();
    if (name !== "capture" && CMX.capture) CMX.capture.stopFullscreen();
    if (name !== "media") media.release();
  }

  function applyVolume(el, ownMuted) {
    el.volume = Math.max(0, Math.min(1, state.volume / 100));
    el.muted = !!(state.muted || ownMuted);
  }

  function fmtTime(s) {
    if (!isFinite(s) || s < 0) s = 0;
    const m = Math.floor(s / 60), r = Math.floor(s % 60);
    return `${m}:${r < 10 ? "0" : ""}${r}`;
  }

  // ---------------------------------------------------------------------
  // Media: two layers crossfaded by opacity; a preloaded next video
  // ---------------------------------------------------------------------
  const media = (() => {
    const layers = [$("layerA"), $("layerB")];
    let active = 0;
    let cur = null;            // { gen, el, kind, start, end, loop, ended }
    const preloaded = new Map();   // url -> <video> buffering in the background

    function makeVideo(url, start) {
      const v = document.createElement("video");
      v.preload = "auto";
      v.playsInline = true;
      v.src = url;
      if (start) v.addEventListener("loadedmetadata", () => { try { v.currentTime = start; } catch (e) {} }, { once: true });
      return v;
    }

    function preload(p) {
      for (const [u, v] of preloaded) {
        if (!p || u !== p.url) { v.removeAttribute("src"); v.load(); preloaded.delete(u); }
      }
      if (p && p.kind !== "image" && !preloaded.has(p.url)) {
        const v = makeVideo(p.url, p.start || 0);
        v.muted = true;
        preloaded.set(p.url, v);
      }
    }

    function cropStyle(el, crop) {
      const layer = el.parentElement;
      if (!crop) {
        layer.classList.remove("cropped");
        Object.assign(el.style, { width: "100%", height: "100%", left: "0", top: "0" });
        return;
      }
      const [x, y, w, h] = crop;
      layer.classList.add("cropped");
      Object.assign(el.style, {
        width: `${100 / w}%`, height: `${100 / h}%`,
        left: `${-100 * x / w}%`, top: `${-100 * y / h}%`,
      });
    }

    function show(m) {
      const next = 1 - active;
      const layer = layers[next];
      layer.innerHTML = "";
      let el;
      if (m.kind === "image") {
        el = document.createElement("img");
        el.src = m.url;
      } else {
        el = preloaded.get(m.url) || makeVideo(m.url, m.start || 0);
        preloaded.delete(m.url);
        el.loop = false;   // looping is done here so the in-point is honoured
        applyVolume(el, false);
      }
      layer.appendChild(el);
      cropStyle(el, m.crop);
      const old = cur;
      cur = { gen: m.gen, el, kind: m.kind, start: m.start || 0, end: m.end, loop: !!m.loop, ended: false };

      const reveal = () => {
        if (!cur || cur.el !== el) return;
        layers[next].classList.add("on");
        layers[active].classList.remove("on");
        const was = layers[active];
        active = next;
        setTimeout(() => { if (old && old.el && old.el.tagName === "VIDEO") { old.el.pause(); } if (was !== layers[active]) was.innerHTML = ""; }, state.fadeMs + 50);
        loading.hide();
      };
      if (m.kind === "image") {
        if (el.complete && el.naturalWidth) reveal(); else { el.onload = reveal; el.onerror = () => { reveal(); post("error", { gen: m.gen, what: "image failed to load" }); }; }
      } else {
        loading.show();
        el.onended = () => onEnd(el);
        el.onerror = () => { post("error", { gen: m.gen, what: "video failed to load" }); onEnd(el, true); };
        const startPlay = () => { el.play().catch(() => {}); reveal(); };
        if (el.readyState >= 3) startPlay(); else el.addEventListener("canplay", startPlay, { once: true });
      }
      if (m.kind === "audio") music.show(m.title || "", el); else music.hide();
      preload(m.preload);
      if (m.paused) setPaused(true);
    }

    function onEnd(el, failed) {
      if (!cur || cur.el !== el || cur.ended) return;
      if (cur.loop && !failed) { try { el.currentTime = cur.start; el.play().catch(() => {}); } catch (e) {} return; }
      cur.ended = true;
      post("ended", { gen: cur.gen });
    }

    // the out-point and status, checked on the playhead
    setInterval(() => {
      if (!cur || cur.kind === "image") return;
      const el = cur.el;
      if (cur.end != null && !cur.ended && el.currentTime >= cur.end - 0.04) {
        if (cur.loop) { try { el.currentTime = cur.start; } catch (e) {} }
        else { el.pause(); onEnd(el); }
      }
    }, 100);

    function setPaused(p) {
      if (!cur || cur.kind === "image") return;
      if (p) cur.el.pause(); else cur.el.play().catch(() => {});
    }

    function seek(to) { if (cur && cur.kind !== "image") { try { cur.el.currentTime = to; } catch (e) {} } }

    function release() {
      for (const l of layers) {
        for (const v of l.querySelectorAll("video")) { v.pause(); v.removeAttribute("src"); v.load(); }
        l.innerHTML = ""; l.classList.remove("on");
      }
      cur = null;
    }

    function status() {
      if (!cur || cur.kind === "image") return { position: null, duration: null, paused: null };
      const el = cur.el;
      return { position: el.currentTime, duration: isFinite(el.duration) ? el.duration : null, paused: el.paused,
               has_video: el.videoWidth > 0 };
    }

    function volume() { if (cur && cur.kind !== "image") applyVolume(cur.el, false); }
    function crop(c) { if (cur) cropStyle(cur.el, c); }

    return { show, setPaused, seek, release, status, volume, crop, get cur() { return cur; } };
  })();

  // ---------------------------------------------------------------------
  // Now Playing card (audio-only content)
  // ---------------------------------------------------------------------
  const music = (() => {
    const root = $("music"), title = root.querySelector(".title"), span = title.querySelector("span");
    const fill = root.querySelector(".fill"), el = root.querySelector(".el"), tot = root.querySelector(".tot");
    const canvas = $("musicWave"), ctx = canvas.getContext("2d");
    let src = null, timer = null, raf = null, phase = 0;

    function tidy(name) {
      return (name || "").replace(/\.[a-z0-9]{2,4}$/i, "").replace(/[_\-.]+/g, " ").replace(/\s+/g, " ").trim();
    }
    function wave() {
      const w = canvas.width = canvas.clientWidth, h = canvas.height = canvas.clientHeight;
      ctx.clearRect(0, 0, w, h);
      const colors = ["#16EB87", "#2ee6ff", "#b46bff"];
      for (let k = 0; k < 3; k++) {
        ctx.beginPath();
        for (let x = 0; x <= w; x += 8) {
          const y = h / 2 + Math.sin(x / (120 + 40 * k) + phase * (1 + k * .3)) * h * (.08 + .03 * k);
          if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = colors[k]; ctx.lineWidth = 2; ctx.globalAlpha = .6; ctx.stroke();
      }
      phase += .03;
      raf = requestAnimationFrame(wave);
    }
    function show(name, mediaEl) {
      src = mediaEl;
      span.textContent = tidy(name) || "Audio";
      root.classList.add("on");
      requestAnimationFrame(() => title.classList.toggle("marquee", span.offsetWidth > title.clientWidth));
      clearInterval(timer);
      timer = setInterval(() => {
        if (!src) return;
        const c = media.cur, s0 = c ? c.start : 0, e0 = c && c.end != null ? c.end : src.duration;
        const pos = Math.max(0, src.currentTime - s0), len = Math.max(0, (e0 || 0) - s0);
        fill.style.width = len ? `${Math.min(100, 100 * pos / len)}%` : "0";
        el.textContent = fmtTime(pos); tot.textContent = fmtTime(len);
      }, 250);
      cancelAnimationFrame(raf);
      if (state.config.music_wave) wave(); else ctx.clearRect(0, 0, canvas.width, canvas.height);
    }
    function hide() { root.classList.remove("on"); clearInterval(timer); cancelAnimationFrame(raf); src = null; }
    return { show, hide };
  })();

  // ---------------------------------------------------------------------
  // Loading spinner (off unless configured), download panel, OSD, overlay
  // ---------------------------------------------------------------------
  const loading = (() => {
    const el = $("loading"); let backstop = null;
    return {
      show() { if (!state.config.loading_indicator) return; el.classList.add("on"); clearTimeout(backstop); backstop = setTimeout(() => el.classList.remove("on"), 15000); },
      hide() { el.classList.remove("on"); clearTimeout(backstop); },
    };
  })();

  let osdTimer = null;
  function osd(text, ms) {
    const el = $("osd");
    el.textContent = text || "";
    el.classList.toggle("on", !!text);
    clearTimeout(osdTimer);
    if (text && ms) osdTimer = setTimeout(() => el.classList.remove("on"), ms);
  }

  function overlay(m) {
    const el = $("overlay");
    el.className = "";
    if (!m || !m.kind) return;
    el.classList.add("on", m.opaque ? "opaque" : "translucent");
    el.querySelector(".t").textContent = m.title || "";
    el.querySelector(".b").textContent = m.body || "";
  }

  // ---------------------------------------------------------------------
  // The player's own screens: setup, enrollment, messages
  // ---------------------------------------------------------------------
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  function screen(m) {
    const el = $("screen");
    state.kind = "screen";
    if (m.name === "black") { el.innerHTML = ""; }
    else if (m.name === "enrollment") {
      el.innerHTML = `<div class="row"><div>
          <h1>REGISTRATION CODE</h1><div class="code">${esc(m.code)}</div>
          <div class="sub">Enter this code in CM:EX to approve this player</div>
          <div class="muted">${esc(m.server)}</div>
          ${m.site ? `<div class="muted">Site: ${esc(m.site)}</div>` : ""}
          <div class="muted" style="margin-top:4vh">Waiting for approval…</div></div>
        ${m.qr ? `<div><div class="qr"><img src="${esc(m.qr)}" alt=""></div><div class="qrcap">Or scan with Connect:EX</div></div>` : ""}
        </div>`;
    } else if (m.name === "setup") {
      setupForm(el, m);
    } else {
      el.innerHTML = `<div><div class="title">${esc(m.title)}</div><div class="body">${esc(m.body)}</div></div>`;
    }
    showSurface("screen");
  }

  function splitUrl(url, secureDefault) {
    try {
      const u = new URL(url);
      return { https: u.protocol === "https:", domain: u.hostname, port: u.port };
    } catch (e) { return { https: secureDefault, domain: "", port: "" }; }
  }

  function compose(https, domain, port) {
    domain = (domain || "").trim().replace(/^https?:\/\//i, "").replace(/\/.*$/, "");
    if (!domain) return "";
    const p = (port || "").trim();
    const dflt = (https && p === "443") || (!https && p === "80");
    return `${https ? "https" : "http"}://${domain}${p && !dflt ? ":" + p : ""}`;
  }

  function setupForm(el, m) {
    const cloud = splitUrl(m.cloud || "https://cm-ex.interaction.works/", true);
    const edge = splitUrl(m.edge || "", false);
    let mode = m.mode === "edge" ? "edge" : "cloud";
    el.innerHTML = `<form autocomplete="off">
      <h1>CONNECT TO CM:EX</h1>
      <div class="sub" style="text-align:center">Enter your CM:EX server address to register this player.</div>
      <div class="seg"><label data-m="cloud">CLOUD</label><label data-m="edge">EDGE (LAN)</label></div>
      <fieldset class="edge"><legend>EDGE SERVER</legend><div class="line">
        <label class="https"><input type="checkbox" name="esec"> HTTPS</label>
        <input type="text" name="edomain" placeholder="edge server address">
        <input type="text" name="eport" class="port" placeholder="8080" inputmode="numeric"></div></fieldset>
      <fieldset><legend class="cl">CLOUD SERVER</legend><div class="line">
        <label class="https"><input type="checkbox" name="csec"> HTTPS</label>
        <input type="text" name="cdomain" placeholder="cm-ex.interaction.works">
        <input type="text" name="cport" class="port" placeholder="port" inputmode="numeric"></div></fieldset>
      <div class="preview"></div><div class="err">${esc(m.error || "")}</div>
      <button type="submit">CONNECT</button></form>`;
    const f = el.querySelector("form");
    f.csec.checked = cloud.https; f.cdomain.value = cloud.domain; f.cport.value = cloud.port;
    f.esec.checked = edge.https; f.edomain.value = edge.domain; f.eport.value = edge.port;
    const refresh = () => {
      for (const l of f.querySelectorAll(".seg label")) l.classList.toggle("on", l.dataset.m === mode);
      f.querySelector("fieldset.edge").style.display = mode === "edge" ? "block" : "none";
      f.querySelector(".cl").textContent = mode === "edge" ? "CLOUD SERVER (FAILOVER)" : "CLOUD SERVER";
      const c = compose(f.csec.checked, f.cdomain.value, f.cport.value);
      const e = compose(f.esec.checked, f.edomain.value, f.eport.value || "8080");
      f.querySelector(".preview").textContent = mode === "edge" ? `edge ${e || "…"} → cloud ${c || "…"}` : (c || "");
    };
    for (const l of f.querySelectorAll(".seg label")) l.onclick = () => { mode = l.dataset.m; refresh(); };
    f.oninput = refresh;
    for (const p of [f.cport, f.eport]) p.addEventListener("input", () => { p.value = p.value.replace(/\D/g, ""); });
    f.onsubmit = (ev) => {
      ev.preventDefault();
      const err = f.querySelector(".err");
      const c = compose(f.csec.checked, f.cdomain.value, f.cport.value);
      const e = compose(f.esec.checked, f.edomain.value, f.eport.value || "8080");
      for (const p of [f.cport.value, mode === "edge" ? f.eport.value : ""]) {
        if (p && (+p < 1 || +p > 65535)) { err.textContent = "Ports must be between 1 and 65535."; return; }
      }
      if (!c) { err.textContent = mode === "edge" ? "The cloud server is required as the failover." : "Enter the server address."; return; }
      if (mode === "edge" && !f.edomain.value.trim()) { err.textContent = "Enter the edge server address."; return; }
      err.textContent = "Connecting…";
      post("setup", { mode, cms_url: c, edge_url: mode === "edge" ? e : "" });
    };
    refresh();
    setTimeout(() => (mode === "edge" ? f.edomain : f.cdomain).focus(), 50);
  }

  // ---------------------------------------------------------------------
  // Messages from the player
  // ---------------------------------------------------------------------
  on("screen", screen);
  on("media", (m) => {
    state.gen = m.gen; state.kind = m.kind;
    if (state.fadeMs !== (m.fade_ms ?? 400)) { state.fadeMs = m.fade_ms ?? 400; document.documentElement.style.setProperty("--fade", `${state.fadeMs}ms`); }
    showSurfaceKeepMedia();
    media.show(m);
  });
  function showSurfaceKeepMedia() {
    for (const s of SURFACES) {
      if (s === "media") { $(s).style.display = "block"; continue; }
      if (s === "screen") $(s).classList.remove("on"); else $(s).style.display = "none";
    }
    const f = $("web"); if (f.src && f.src !== "about:blank") f.src = "about:blank";
    if (CMX.template) CMX.template.clear();
    if (CMX.capture) CMX.capture.stopFullscreen();
  }
  on("web", (m) => {
    state.gen = m.gen; state.kind = "web";
    if (m.snapshot_only) {
      showSurface("snapshot");
      const s = $("snapshot");
      s.querySelector("img").src = m.snapshot;
      s.querySelector(".badge").textContent = m.badge || "Saved page";
      return;
    }
    showSurface("web");
    const f = $("web");
    if (m.canvas) {
      // a template's fixed-size render page, scaled to fit the screen
      const [cw, ch] = m.canvas, sw = window.innerWidth, sh = window.innerHeight, s = Math.min(sw / cw, sh / ch);
      Object.assign(f.style, { width: `${cw}px`, height: `${ch}px`, transformOrigin: "0 0",
        transform: `translate(${(sw - cw * s) / 2}px, ${(sh - ch * s) / 2}px) scale(${s})` });
    } else {
      Object.assign(f.style, { width: "100%", height: "100%", transform: "" });
    }
    let settled = false;
    f.onload = () => { if (!settled) { settled = true; post("web_loaded", { gen: m.gen, ok: true }); } loading.hide(); };
    loading.show();
    f.src = m.url;
    if (m.snapshot) {
      setTimeout(() => {
        if (settled || state.gen !== m.gen) return;
        settled = true;
        showSurface("snapshot");
        const s = $("snapshot");
        s.querySelector("img").src = m.snapshot;
        s.querySelector(".badge").textContent = m.badge || "Saved page";
        post("web_loaded", { gen: m.gen, ok: false });
      }, (m.timeout_s || 30) * 1000);
    }
  });
  on("pause", (m) => { media.setPaused(!!m.paused); if (CMX.template) CMX.template.setPaused(!!m.paused); });
  on("seek", (m) => media.seek(m.to));
  on("stop", () => { state.gen = -1; state.kind = null; $("screen").innerHTML = ""; showSurface("screen"); });
  on("crop", (m) => media.crop(m.crop));
  on("volume", (m) => {
    state.volume = m.volume; state.muted = !!m.muted;
    media.volume();
    if (CMX.template) CMX.template.volume();
    if (CMX.capture) CMX.capture.volume();
  });
  on("config", (m) => { Object.assign(state.config, m); if (!state.config.loading_indicator) loading.hide(); });
  on("osd", (m) => osd(m.text, m.ms));
  on("overlay", overlay);
  on("download", (m) => {
    const el = $("download");
    if (!m || !m.total || m.done >= m.total) { el.classList.remove("on"); return; }
    el.classList.add("on");
    el.querySelector(".t").textContent = `Downloading ${m.done + 1}/${m.total}…`;
    el.querySelector(".fill").style.width = `${Math.round(100 * (m.fraction || 0))}%`;
  });
  on("reload", () => location.reload());

  function dispatch(m) {
    const fn = handlers[m.type];
    if (!fn) return;
    try { fn(m); } catch (e) { post("error", { gen: m.gen, what: `${m.type}: ${e && e.message}` }); }
  }

  function connect() {
    const es = new EventSource("/events");
    es.onmessage = (ev) => { try { dispatch(JSON.parse(ev.data)); } catch (e) {} };
    es.onopen = () => post("ready", {});
  }

  // status (position) twice a second, liveness every 2 s
  setInterval(() => {
    let st;
    if (state.kind === "template" && CMX.template) st = { position: null, duration: null, paused: null };
    else st = media.status();
    post("status", Object.assign({ gen: state.gen, kind: state.kind }, st));
  }, 500);
  setInterval(() => post("alive", {}), 2000);

  // Reset registration on the screen: Ctrl+Alt+Shift+R, then Y within 10 s.
  let resetArmed = null;
  document.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey && ev.altKey && ev.shiftKey && (ev.key === "R" || ev.key === "r")) {
      clearTimeout(resetArmed);
      overlay({ kind: "reset", opaque: true, title: "Reset this player's registration?",
                body: "It forgets its CM:EX registration and cached content. Press Y to confirm, any other key to cancel." });
      resetArmed = setTimeout(() => { resetArmed = null; overlay(null); }, 10000);
      ev.preventDefault();
      return;
    }
    if (resetArmed) {
      clearTimeout(resetArmed); resetArmed = null; overlay(null);
      if (ev.key === "y" || ev.key === "Y") post("reset_registration", {});
      ev.preventDefault();
    }
  });

  document.addEventListener("DOMContentLoaded", connect);

  return { on, post, state, showSurface, applyVolume, esc, loading, media, fmtTime };
})();

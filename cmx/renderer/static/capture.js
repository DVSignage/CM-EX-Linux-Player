/*
 * Live capture (USB / UVC through the browser's camera API): full screen
 * (show_capture) and inside templates (capture elements). Reports the capture
 * devices to the player for the heartbeat's capture_devices list.
 *
 * A/V trim (av_offset_ms, ±500 ms), DVSI Plus's convention: positive HOLDS THE
 * PICTURE (frames are buffered and drawn late on a canvas), negative delays the
 * sound (a Web Audio delay line). Changed live, without reopening the device.
 */
"use strict";

CMX.capture = (() => {
  const root = document.getElementById("capture");
  const fsVideo = root.querySelector("video"), fsHint = root.querySelector(".hint");
  let fsSlot = null;
  const slots = new Set();
  const MAX_TRIM = 500;
  let audioCtx = null;

  async function pickDevice(label, id) {
    const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
    if (id) { const byId = devs.find((d) => d.deviceId === id); if (byId) return byId; }
    if (label) {
      const l = label.toLowerCase();
      const byLabel = devs.find((d) => d.label.toLowerCase() === l) || devs.find((d) => d.label.toLowerCase().includes(l));
      if (byLabel) return byLabel;
    }
    return devs[0] || null;
  }

  // Ask the camera for what the picture covers on screen, at most capture_max_height
  // lines (720 by default, config.yaml): every frame of a USB camera is decoded and
  // drawn by the CPU, and a Raspberry Pi 4 reached 85 °C and throttled decoding 1080p
  // for a template tile. A camera answers with its nearest mode (a C920 gives
  // 800×448 for an 800×450 tile).
  function wantedSize(video) {
    const maxH = Math.max(240, +(CMX.state.config.capture_max_height) || 720);
    const maxW = Math.round(maxH * 16 / 9);
    const r = video.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    let w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);
    if (!(w > 0 && h > 0)) { w = maxW; h = maxH; }      // not laid out yet: the cap itself
    const k = Math.min(1, maxW / w, maxH / h);
    return { width: Math.max(320, Math.round(w * k)), height: Math.max(180, Math.round(h * k)) };
  }

  function clampTrim(ms) { ms = +ms || 0; return Math.max(-MAX_TRIM, Math.min(MAX_TRIM, Math.round(ms))); }

  // One consumer of a camera: keeps it open, shows "waiting / lost" in its
  // own hint, retries when the signal goes away, applies the A/V trim.
  function attach(video, hint, label, audio, id, trimMs) {
    let stream = null, stopped = false, retry = null, waitTimer = null, everFrame = false;
    let trim = clampTrim(trimMs);
    let gain = null, delay = null, source = null;       // the audio path (Web Audio)
    let canvas = null, frames = [], drawing = false;   // the held picture
    const slot = {
      stop() {
        stopped = true; clearTimeout(retry); clearTimeout(waitTimer);
        if (stream) stream.getTracks().forEach((t) => t.stop());
        stream = null; video.srcObject = null; slots.delete(slot);
        try { if (source) source.disconnect(); } catch (e) {}
        hold(0);
      },
      volume() {
        // sound goes through the Web Audio path; the element itself stays silent
        video.muted = true;
        if (gain) gain.gain.value = (!audio || CMX.state.muted) ? 0 : Math.max(0, Math.min(1, CMX.state.volume / 100));
      },
      setTrim(ms) { trim = clampTrim(ms); applyTrim(); },
      get trim() { return trim; },
    };
    slots.add(slot);
    const say = (text) => { hint.textContent = text || ""; hint.classList.toggle("on", !!text); };

    function audioPath() {
      if (!audio || !stream || !stream.getAudioTracks().length) return;
      try {
        audioCtx = audioCtx || new AudioContext();
        if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
        source = audioCtx.createMediaStreamSource(stream);
        delay = audioCtx.createDelay(1.0);
        gain = audioCtx.createGain();
        source.connect(delay).connect(gain).connect(audioCtx.destination);
      } catch (e) { gain = delay = source = null; }
    }

    // positive trim: draw each frame `trim` ms late from a short buffer
    function hold(ms) {
      if (ms <= 0) {
        drawing = false; frames.forEach((f) => f.bmp.close && f.bmp.close()); frames = [];
        if (canvas) { canvas.remove(); canvas = null; }
        video.style.opacity = "";
        return;
      }
      if (!canvas) {
        canvas = document.createElement("canvas");
        canvas.style.cssText = `position:absolute;inset:0;width:100%;height:100%;object-fit:${video.style.objectFit || "contain"}`;
        video.after(canvas);
        video.style.opacity = "0";
      }
      if (drawing) return;
      drawing = true;
      const grab = () => {
        if (!drawing || stopped) return;
        if (video.videoWidth) {
          createImageBitmap(video).then((bmp) => { frames.push({ t: performance.now(), bmp }); }).catch(() => {});
        }
        if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(grab); else setTimeout(grab, 33);
      };
      const draw = () => {
        if (!drawing || stopped) return;
        const due = performance.now() - trim;
        let shown = null;
        while (frames.length && frames[0].t <= due) { if (shown) shown.bmp.close && shown.bmp.close(); shown = frames.shift(); }
        if (shown && canvas) {
          if (canvas.width !== shown.bmp.width) { canvas.width = shown.bmp.width; canvas.height = shown.bmp.height; }
          canvas.getContext("2d").drawImage(shown.bmp, 0, 0);
          shown.bmp.close && shown.bmp.close();
        }
        while (frames.length > 60) { const f = frames.shift(); f.bmp.close && f.bmp.close(); }
        requestAnimationFrame(draw);
      };
      grab(); draw();
    }

    function applyTrim() {
      if (delay) delay.delayTime.value = trim < 0 ? -trim / 1000 : 0;
      hold(trim > 0 ? trim : 0);
      CMX.post("capture_trim", { av_offset_ms: trim, av_hold_ms: Math.max(0, trim) });
    }

    async function open() {
      if (stopped) return;
      try {
        const dev = await pickDevice(label, id);
        if (!dev) throw new Error("no capture device");
        const size = wantedSize(video);
        stream = await navigator.mediaDevices.getUserMedia({
          video: { deviceId: { exact: dev.deviceId }, width: { ideal: size.width }, height: { ideal: size.height } },
          audio: audio ? true : false,
        });
        if (stopped) { stream.getTracks().forEach((t) => t.stop()); return; }
        video.srcObject = stream;
        audioPath();
        slot.volume();
        applyTrim();
        video.play().catch(() => {});
        waitTimer = setTimeout(() => { if (!video.videoWidth) say(everFrame ? "Signal lost — reconnecting…" : "Waiting for signal…"); }, 2500);
        video.onplaying = () => { everFrame = true; say(""); CMX.loading.hide(); };
        for (const t of stream.getVideoTracks()) t.onended = () => { say("Signal lost — reconnecting…"); retry = setTimeout(open, 3000); };
        reportDevices();
      } catch (e) {
        say(everFrame ? "Signal lost — reconnecting…" : "Waiting for signal…");
        retry = setTimeout(open, 3000);
      }
    }
    open();
    return slot;
  }

  async function reportDevices() {
    try {
      const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
      CMX.post("devices", { capture: devs.map((d, i) => ({ label: d.label || `Camera ${i + 1}`, deviceId: d.deviceId })) });
    } catch (e) { /* no camera API */ }
  }

  CMX.on("capture", (m) => {
    if (fsSlot) fsSlot.stop();
    CMX.state.gen = m.gen; CMX.state.kind = "capture";
    CMX.showSurface("capture");
    CMX.loading.show();
    fsSlot = attach(fsVideo, fsHint, m.device_label, !!m.audio, m.device_id, m.av_offset_ms);
  });
  // the operator's trim changed: applied live, the device stays open
  CMX.on("capture_trim", (m) => { if (fsSlot) fsSlot.setTrim(m.av_offset_ms); });

  if (navigator.mediaDevices) {
    navigator.mediaDevices.addEventListener("devicechange", reportDevices);
    setTimeout(reportDevices, 1500);
    setInterval(reportDevices, 30000);
  }

  return {
    attach,
    clampTrim,
    stopFullscreen() { if (fsSlot) { fsSlot.stop(); fsSlot = null; } },
    volume() { for (const s of slots) s.volume(); },
  };
})();

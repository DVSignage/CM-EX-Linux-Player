/*
 * Live capture (USB / UVC through the browser's camera API): full screen
 * (show_capture) and inside templates (capture elements). Reports the capture
 * devices to the player for the heartbeat's capture_devices list.
 */
"use strict";

CMX.capture = (() => {
  const root = document.getElementById("capture");
  const fsVideo = root.querySelector("video"), fsHint = root.querySelector(".hint");
  let fsSlot = null;
  const slots = new Set();

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

  // One consumer of a camera: keeps it open, shows "waiting / lost" in its
  // own hint, retries when the signal goes away.
  function attach(video, hint, label, audio, id) {
    let stream = null, stopped = false, retry = null, waitTimer = null, everFrame = false;
    const slot = {
      stop() {
        stopped = true; clearTimeout(retry); clearTimeout(waitTimer);
        if (stream) stream.getTracks().forEach((t) => t.stop());
        stream = null; video.srcObject = null; slots.delete(slot);
      },
      volume() { CMX.applyVolume(video, !audio); },
    };
    slots.add(slot);
    const say = (text) => { hint.textContent = text || ""; hint.classList.toggle("on", !!text); };
    async function open() {
      if (stopped) return;
      try {
        const dev = await pickDevice(label, id);
        if (!dev) throw new Error("no capture device");
        stream = await navigator.mediaDevices.getUserMedia({
          video: { deviceId: { exact: dev.deviceId }, width: { ideal: 1920 }, height: { ideal: 1080 } },
          audio: audio ? true : false,
        });
        if (stopped) { stream.getTracks().forEach((t) => t.stop()); return; }
        video.srcObject = stream;
        slot.volume();
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
    fsSlot = attach(fsVideo, fsHint, m.device_label, !!m.audio, m.device_id);
  });

  if (navigator.mediaDevices) {
    navigator.mediaDevices.addEventListener("devicechange", reportDevices);
    setTimeout(reportDevices, 1500);
    setInterval(reportDevices, 30000);
  }

  return {
    attach,
    stopFullscreen() { if (fsSlot) { fsSlot.stop(); fsSlot = null; } },
    volume() { for (const s of slots) s.volume(); },
  };
})();

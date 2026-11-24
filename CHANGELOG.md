# Changelog

All notable changes to the CM:EX Linux Player.

## [2.1.1] - 2026-11-24

Checked against the real CM:EX server: 16 real template definitions, the
interactive walls they carry, the CMS's own render page, and the inventory a
2.1.0 player already reported.

### Added
- Native `web` elements (embedded pages) in templates, and the template
  background image (`background_image_id`).
- Ubuntu desktop / Wayland: the player waits for the desktop session, finds
  the XWayland auth file at every start (its name changes at each login),
  keeps Chromium on X11, and keeps GNOME from blanking, locking or showing
  notification banners. `install.sh --autologin` logs the desktop user in at
  boot (GDM / LightDM).
- Preview frames taken from Chromium itself (DevTools, loopback only), so
  previews work where an X screen grab is black (XWayland).
- Remote-access inventory: service state (running / stopped / absent),
  package versions, CM:EX tool ids (realvnc, wayvnc, x11vnc, tigervnc...).
- Real template definitions (scrubbed) as test fixtures.

### Fixed
- Template rendering matched to the CMS render page: canvas colour from
  `background_color`, text `verticalAlign` and padding, a "No image"
  placeholder, RSS `speedPxS`, `separator`, `backgroundOpacity`, `showImages`
  and the CMS's row and ticker styling.
- SSH key-only logins were reported with an auth value the CMS does not know.

## [2.1.0] - 2026-11-23

The rest of the DVSI Plus CMX player's feature set. What is on screen is now
drawn by a renderer page the player serves itself (port 8083, loopback) in a
Chromium kiosk; mpv stays for multicast wall streams and as the fallback when
Chromium is missing.

### Added
- Crossfades between items, with the next video preloaded; an audio-only
  Now Playing card; an optional loading spinner and a download panel.
- On-screen setup (cloud, or edge server + cloud failover) when no `cms_url`
  is configured.
- Native templates drawn from cached files, offline: text, panels, pictures,
  clips, RSS ticker/list from a cached feed, capture elements, trigger zones
  and nested templates (3 levels). Unknown element types use the CMS render.
- `template_patch` (one element changed live) and in-place merge of an
  edited template.
- Interactive walls: a port of DVSI Plus's InteractionEngine — triggers from
  the CMS or touch, scenes, attractors, layouts, retrigger policies, back
  history, idle return, scheduled home scenes; `trigger`, `set_scene`,
  `wall_play`, `alignment`; `scene_id`/`home_scene_id` in the heartbeat.
- Live capture: `show_capture`/`hide_capture` for USB/UVC devices, capture
  device list in the heartbeat.
- Edge (LAN) mode: edge-first routing with cloud failover and failback,
  edge-404 confirmation with the cloud, site check, failover telemetry.
- Proof of play and player health telemetry (edge mode).
- Machine inventory reporting (OS, remote-access tools, installed apps).
- Chromium crash and hang recovery with `renderer_crash` telemetry.
- Instant reconnect when the network changes while offline.
- Saved-page snapshot for web items that cannot load; a separate window
  for sites that refuse to be framed.

### Changed
- `install.sh` installs Chromium and DejaVu fonts.

## [2.0.0] - 2026-11-23

Brought to parity with the DVSI Plus CMX player (core tier) and restructured
into the `cmx/` package.

### Added
- Player secret: saved from the enrollment approval and sent as
  `X-Player-Secret`; `state.json` is now owner-only.
- Registration screen with a Connect:EX QR code; enrollment keeps the same
  code through 404/429/503 answers (10-minute cap per code).
- Command envelope: `command_id` commands run once and every delivery is
  acknowledged with its real result; unsupported commands are refused with a
  reason.
- SSE push channel (`/players/{id}/events`).
- New commands: `stop`, `replay`, `seek`, `refresh`, `set_volume`,
  `delete_player`, `load_wall_content` (download + crop); heartbeat desired
  state (`audio_volume`, `audio_muted`, `desired_blank`).
- Heartbeat: `playback_status`, clip-relative `position`/`duration`,
  `current_content_id`, an `error` line for real faults, device lists.
- Clip in/out (`clip_start`/`clip_end`), single-item loop in place, identical
  re-applies keep the playback position.
- Templates load `/templates/{id}/render` scaled to the screen; RSS loads
  `/rss/{id}/render`; web aliases (`webpage`, `iframe`, `link`, `embed`, `web`).
- Cache: files keyed `{content_id}{ext}`, picture/video decided by the file's
  bytes, Range resume, background retries for assets not ready yet, the last N
  playlists kept cached (`cache.keep_playlists`).
- Pushed live preview (`preview_watch` → `/players/{id}/preview-frame`).
- Telemetry queue with `boot` and `crash` events.
- Offline handling: 15 s reconnect polling, status screens, the playlist is
  refetched when the CMS comes back.
- Test suite against the real code, plus `tests/fake_cms.py`.

### Fixed
- `restart_service` did nothing: `sudo systemctl` is blocked by
  `NoNewPrivileges=true`. The player now exits after the ack and systemd
  restarts it (`RestartSec` 10 → 3).
- mpv could stay black on machines without working GL: the video output is now
  a fallback list (`gpu,x11,drm`) chosen when mpv opens it.
- Runtime mpv properties were read through python-mpv's options namespace and
  always failed, so the wall-stream death detection never fired.
- Shutdown raised "Event loop stopped before Future completed".
- `log_level` and `playback.default_image_duration` were ignored.
- `setup.py` pointed at a missing `main:main`.
- `install.sh` aborted when the optional NDI packages had no wheel; NDI moved
  to `requirements-ndi.txt` and installs best-effort.
- The offline installer and package builder missed `psutil`.
- `deploy-update.ps1` only pushed `main.py`, its `ssh` call never splatted
  its arguments, and it did not parse in Windows PowerShell 5.1.

### Removed
- Unused legacy modules (`api_client/`, `config/`, `core/`, `network/`,
  `utils/`, `dev_tools.py`) and the tests that imported a never-committed
  `cache/` package.

## [1.0.0] - 2024

Initial release.

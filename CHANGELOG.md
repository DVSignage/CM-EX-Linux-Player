# Changelog

All notable changes to the CM:EX Linux Player.

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

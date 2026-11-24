# CM:EX Linux Player

A native Linux media player daemon for CM:EX. Uses [mpv](https://mpv.io/) for hardware-accelerated fullscreen playback, downloads content directly from the CMS API, and enrolls itself with a PIN code displayed on screen -- no manual ID configuration needed.

## Quick Install (one command)

```bash
curl -sSL https://raw.githubusercontent.com/DVSignage/CM-EX-Linux-Player/main/install.sh | sudo bash
```

This single command will install all dependencies, create a system service, and start the player. You will be prompted to enter your CMS URL during installation.

---

## How it works

1. On first boot the player shows a **6-digit enrollment code** full-screen
2. In the CMS portal go to **Players > Enrol Player** and enter the code
3. Once approved the player saves its identity and starts playing immediately
4. Content is downloaded from the CMS and cached locally -- playback continues if the network drops

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| Linux (Ubuntu 20.04+ recommended) | Debian, Fedora, Arch also supported |
| Python 3.10 or higher | `python3 --version` |
| Chromium | Draws templates, walls, crossfades and the player's screens (installed by `install.sh`; without it the player runs mpv-only) |
| mpv 0.33+ with libmpv | Multicast wall streams, and the whole display when Chromium is missing |
| X11 or Wayland display server | Must have a display |

---

## Installation

### Automated (recommended)

The install script detects your distro (Ubuntu/Debian, Fedora/RHEL, Arch), installs everything, and starts the service:

```bash
curl -sSL https://raw.githubusercontent.com/DVSignage/CM-EX-Linux-Player/main/install.sh | sudo bash
```

Or if you have already cloned the repo:

```bash
sudo bash install.sh
```

The script is idempotent -- running it again will update player files and pip packages without losing your configuration or enrollment state.

### Manual install

If you prefer to install step by step:

#### 1. Install system dependencies

**Ubuntu / Debian:**
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv mpv libmpv-dev
```

**Fedora / RHEL:**
```bash
sudo dnf install -y python3 python3-pip python3-virtualenv mpv mpv-libs-devel
```

**Arch Linux:**
```bash
sudo pacman -S --needed python python-pip mpv
```

#### 2. Get the code

```bash
git clone https://github.com/DVSignage/CM-EX-Linux-Player.git
cd CM-EX-Linux-Player
```

#### 3. Create the install directory and copy files

```bash
sudo mkdir -p /opt/signage-player
sudo cp -r . /opt/signage-player/
```

#### 4. Create a Python virtual environment

```bash
sudo python3 -m venv /opt/signage-player/venv
sudo /opt/signage-player/venv/bin/pip install --upgrade pip
sudo /opt/signage-player/venv/bin/pip install -r /opt/signage-player/requirements.txt
```

#### 5. Create a system user

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin signage
sudo chown -R signage:signage /opt/signage-player
sudo mkdir -p /home/signage/.config/signage-player
sudo chown -R signage:signage /home/signage/.config/signage-player
```

#### 6. Configure

```bash
sudo cp /opt/signage-player/config.yaml.example /opt/signage-player/config.yaml
sudo nano /opt/signage-player/config.yaml
```

Set `cms_url` to your CMS server address (see [Configuration](#configuration) below).

#### 7. Install and start the systemd service

```bash
# Update ExecStart to use the venv python
sudo sed 's|^ExecStart=.*|ExecStart=/opt/signage-player/venv/bin/python3 /opt/signage-player/main.py|' \
    /opt/signage-player/signage-player.service > /etc/systemd/system/signage-player.service

sudo systemctl daemon-reload
sudo systemctl enable signage-player
sudo systemctl start signage-player
```

#### 8. Verify

```bash
sudo systemctl status signage-player
```

---

## Configuration

The configuration file lives at `/opt/signage-player/config.yaml`.

**The only required setting is `cms_url`** -- point it at your CMS server:

```yaml
cms_url: "http://192.168.1.100:8000"
```

Full config reference:

```yaml
# CMS server base URL (required)
cms_url: "http://cms-server:8000"

# Playback settings (all optional)
playback:
  display: ":0"              # X display -- leave as :0 for the primary screen
  audio_output: "auto"       # "auto" lets mpv choose, or use a device name
  default_image_duration: 10 # seconds to show an image when duration is not set

cache:
  keep_playlists: 10         # recent playlists kept cached (1-100)

log_level: "INFO"            # DEBUG, INFO, WARNING, ERROR
```

Player identity (`player_id`) is saved automatically after enrollment in `/home/signage/.config/signage-player/state.json`. You do not need to set it manually.

After changing the config, restart the service:

```bash
sudo systemctl restart signage-player
```

---

## Useful commands

```bash
# View live logs
sudo journalctl -u signage-player -f

# Restart the player
sudo systemctl restart signage-player

# Stop the player
sudo systemctl stop signage-player

# Disable autostart on boot
sudo systemctl disable signage-player

# Check status
sudo systemctl status signage-player
```

---

## Updating

Re-run the installer to pull the latest version and update in place:

```bash
curl -sSL https://raw.githubusercontent.com/DVSignage/CM-EX-Linux-Player/main/install.sh | sudo bash
```

Or from a local clone:

```bash
git pull origin main
sudo bash install.sh
```

Your `config.yaml` and enrollment state are preserved across updates.

---

## Uninstall

To completely remove the player:

```bash
# Stop and disable the service
sudo systemctl stop signage-player
sudo systemctl disable signage-player

# Remove the service file
sudo rm /etc/systemd/system/signage-player.service
sudo systemctl daemon-reload

# Remove the install directory
sudo rm -rf /opt/signage-player

# Remove the signage user and its home directory (including cached media)
sudo userdel -r signage

# (Optional) Remove system packages that were installed
# Ubuntu/Debian:  sudo apt remove mpv libmpv-dev
# Fedora/RHEL:    sudo dnf remove mpv mpv-libs-devel
# Arch:           sudo pacman -R mpv
```

---

## Auto-start on desktop login (alternative to systemd)

If the machine runs a desktop environment (GNOME, KDE, LXDE, etc.) and you want the player to start when the user logs in rather than as a system service:

```bash
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/signage-player.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Signage Player
Exec=/opt/signage-player/venv/bin/python3 /opt/signage-player/main.py
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
```

---

## File locations

| Path | Contents |
|------|----------|
| `/opt/signage-player/` | Player code and virtual environment |
| `/opt/signage-player/config.yaml` | CMS URL and playback settings |
| `/opt/signage-player/venv/` | Python virtual environment |
| `~/.config/signage-player/state.json` | Player identity: `player_id`, `player_secret`, site, volume (owner-only, 0600) |
| `~/.config/signage-player/cache/` | Downloaded media, named `{content_id}{ext}` |
| `~/.config/signage-player/cache-history.json` | File sets of the last N playlists kept in the cache |
| `~/.config/signage-player/offline_playlist.json` | Last applied playlist, played instantly at boot |
| `~/.config/signage-player/telemetry-queue.json` | Telemetry events waiting to be delivered |
| `~/.config/signage-player/crash/` | Crash records, reported at the next start |
| `~/.config/signage-player/local-api.key` | Key for the local preview routes (0600) |
| `/etc/systemd/system/signage-player.service` | Systemd service unit |

`~` is the home of the user the service runs as (the desktop user chosen by `install.sh`).

---

## Re-enrolling a player

If you need to register the player with a different CMS, or the player record was deleted:

- **At the screen:** press **Ctrl+Alt+Shift+R**, then **Y** within 10 seconds.
- **From this machine:**
  `curl -X POST http://127.0.0.1:8083/api/reset-registration -H "X-Local-Key: $(cat ~/.config/signage-player/local-api.key)"`

Either one clears the registration, cache and offline copy. A player whose
`config.yaml` sets `cms_url` shows a new enrollment code; otherwise it returns
to the setup screen. Or by hand:

```bash
sudo rm /home/signage/.config/signage-player/state.json
sudo systemctl restart signage-player
# The player will show a new enrollment code
```

---

## Troubleshooting

### Player shows enrollment code but CMS says "code not found"

- Make sure `cms_url` in `config.yaml` points to the correct server (no trailing slash, correct port)
- Verify the CMS is reachable: `curl http://your-cms:8000/api/v1/health`
- Check the player logs for connection errors: `sudo journalctl -u signage-player -n 50`

### Black screen after enrollment

- The player will start downloading content -- wait a moment for the first file to finish
- Check logs to see download progress: `sudo journalctl -u signage-player -f`
- Make sure the playlist assigned in the CMS has at least one content item

### No video output / mpv window does not appear

- Confirm the display is set correctly: `echo $DISPLAY` should return `:0`
- If running as a service, make sure the `DISPLAY` and `XAUTHORITY` environment variables are set in the service file
- Test mpv independently: `mpv /path/to/video.mp4`
- On headless servers mpv requires a virtual display -- consider using the web player instead

### `ModuleNotFoundError: No module named 'mpv'`

```bash
sudo /opt/signage-player/venv/bin/pip install python-mpv
# If it still fails, ensure libmpv is installed:
sudo apt install libmpv-dev   # Debian/Ubuntu
sudo dnf install mpv-libs-devel  # Fedora
```

### `ImportError: libmpv.so.1: cannot open shared object file`

The shared library is installed but not on the path:
```bash
sudo ldconfig
```

### Content plays but is out of date

The player caches content locally. To force a fresh download, clear the cache:
```bash
sudo rm -rf /home/signage/.config/signage-player/cache/
sudo rm -f /home/signage/.config/signage-player/offline_playlist.json
sudo systemctl restart signage-player
```

### Player was deleted from the CMS portal

The player detects a 404 on the next heartbeat, clears its identity, and immediately shows a new enrollment code so it can be re-registered.

---

## Architecture

The player follows the **DVSI Plus CMX player** (the native Windows port in
`DVSignage/win-web-wrap`) for its CMS contract and behaviour. What is on screen
is drawn by a **renderer page** the player serves itself (port 8090, loopback
only) and shows in a Chromium kiosk, so templates, walls and crossfades work
offline. mpv plays the multicast wall stream, and is the whole display when
Chromium is not installed (`playback.renderer: mpv` forces that).

```
main.py                 # entry point: config, logging, crash hook, run
cmx/
├── player.py           # orchestrator: setup, enrollment, heartbeat, SSE, commands
│                       # + acks, playlists, templates, walls, capture, telemetry
├── surface.py          # PageSurface (renderer page) / MpvSurface (mpv fallback)
├── renderer/server.py  # the renderer page's server: page, media (Range), push, events
├── renderer/static/    # renderer.js (screens, crossfades, Now Playing), template.js
│                       # (native templates, RSS), wall.js (zones), capture.js
├── kiosk.py            # the Chromium kiosk and its crash recovery
├── playback.py         # playlist engine (+ the mpv wrapper)
├── templates.py        # template definition → page payload, nested, template_patch
├── interaction.py      # interactive-wall engine (port of DVSI InteractionEngine)
├── wallhost.py         # the wall engine's screen side
├── cms_client.py       # CM:EX REST client (timeouts, secret header, Range resume)
├── edge.py             # edge (LAN) mode routing and failover
├── commands.py         # command envelope: execute once, ack every delivery
├── items.py            # playlist item classification, dwell, clip in/out
├── cache.py            # {content_id}{ext} cache, byte sniffing, retention, retries
├── telemetry.py        # spooled telemetry queue, boot/crash events
├── inventory.py        # machine inventory
├── faults.py           # the heartbeat "error" line
├── health.py           # metrics, LAN address, network changes
├── preview.py          # pushed live preview
├── wall.py             # wall crop filters, multicast wall stream settings
├── screens.py          # PNG screens for the mpv fallback
├── browser.py          # a separate Chromium window (sites that refuse framing)
├── lan_api.py          # control API on port 8083 (LAN sensors, controllers)
├── ndi_out.py          # this screen as an NDI source
├── local_api.py        # local API on port 8081
└── config.py           # config.yaml, paths, state.json
ndi/engine.py           # optional NDI receive/send
tests/                  # pytest suite + fake_cms.py
```

Run the tests with `./run_tests.sh` (or `python3 -m pytest -q`); they need no
mpv, display or browser.

---

## How it works

**Setup.** With no `cms_url` in `config.yaml` the screen asks for the CM:EX
address (cloud, or edge server + cloud failover); it is saved in `state.json`.

**Enrollment.** The player requests a code and shows it with a Connect:EX QR
code (`dvsiconnect://enroll?...`). It keeps polling with the same code through
404/429/503 answers and only asks for a new one when the code expires, is
rejected, or nobody approves it for 10 minutes. The approval's `player_secret`
is saved and sent as `X-Player-Secret` on every player request.

**Startup.** `offline_playlist.json` plays from the cache straight away, then
one heartbeat (bounded to 5 s) decides: registered → normal routines; 404 →
re-enroll; unreachable → keep playing offline.

**Heartbeat.** Every 500 ms online, every 15 s while offline (a network change
retries at once). It reports playback state, clip-relative position/duration,
what is on screen, the wall's scene, capture devices, an `error` line for real
faults, system metrics and the LAN address. A 404 means the player was deleted:
identity, cache and offline copy are wiped and a new code is shown.

**Commands** arrive on the heartbeat and on the SSE stream
(`/players/{id}/events`). A command with a `command_id` runs once and every
delivery is acknowledged (`POST /players/{id}/ack`) with its real result;
commands this player doesn't implement are refused with a reason. Supported:
`load_playlist`, `load_content`, `load_wall_content`, `load_wall_rtp`,
`stop_wall`, `play`, `pause`, `stop`, `replay`/`restart`, `next`, `previous`,
`seek`, `refresh`, `set_volume`, `delete_player`, `restart_service`,
`trigger`, `set_scene`, `wall_play`, `alignment`, `template_patch`,
`show_capture`, `hide_capture` and the NDI commands. Desired state on the
heartbeat (`audio_volume`, `audio_muted`, `desired_blank`) is applied too.

**Content delivery.**

```
load_playlist (new playlist_hash)
  └─ GET /api/v1/players/{id}/assigned-playlist
       ├─ save offline_playlist.json
       ├─ template → its definition, drawn natively by the page from cached media
       │             (unknown element types → the CMS render page instead)
       ├─ url/web  → content.url in the page (or its own window if the site
       │             refuses framing; its saved snapshot when unreachable)
       ├─ rss      → /api/v1/rss/{id}/render
       └─ media    → cache/{content_id}{ext}
            ├─ cached → play (crossfaded, the next video preloaded)
            └─ GET /api/v1/content/{id}/stream (resumes a partial .tmp with Range)
                 ├─ first item ready → start playing
                 ├─ 503 / failure → retried in the background (30 s … 30 min)
                 └─ the cache keeps the last N playlists (cache.keep_playlists)
```

An identical playlist re-sent by the CMS keeps playing where it is; an edited
template merges in place. A single-item playlist loops in place.
`clip_start`/`clip_end` trim videos. Audio-only items show a Now Playing card.

**Templates** are drawn natively: text, panels, pictures, clips, RSS (ticker or
list, from a cached feed), live capture, trigger zones and nested templates
(three levels). Every file a template can show is downloaded before it goes on
screen. `template_patch` changes one element live.

**Interactive walls** follow the template's `interaction` spec exactly as DVSI
Plus does: triggers (CMS command or touch), scenes with attractors and layouts,
retrigger policies, `then` / back history, idle return and scheduled home
scenes in the install's timezone.

**Control API (port 8083).** The same local API as DVSI Plus, for sensors,
touch panels and show controllers on the venue network. Wall routes:
`POST /api/trigger` (`{"command":"trigger","id":3}` or `?id=3`), `/api/scene`
(`{"scene":"…","stop":true}`), `/api/home`, `/api/back`, `/api/play-content`
(`{"content_id":"…"}`), `/api/alignment` (`{"enabled":true}`), `/api/patch`
(`{"element_id":"…","text":"…"}` or `"content_id"`) and `GET /api/interaction`;
every answer is `{accepted, reason, scene}`. It listens on this machine only
unless `lan_api.enabled` is true; set `lan_api.trigger_key` and LAN or browser
callers must send it as `X-Trigger-Key`. Play/pause/restart, device and
preview routes are for this machine only and need `X-Local-Key` (the key in
`~/.config/signage-player/local-api.key`).

```bash
curl -X POST http://SCREEN-IP:8083/api/trigger -H 'X-Trigger-Key: …' -d '{"command":"trigger","id":3}'
```

**NDI output.** The screen can be published as an NDI source: switch it on
locally (`ndi.output: true`) or from the CMS (`ndi_broadcast_active`, or the
start/stop broadcast commands). As on DVSI Plus, a CMS "off" stops only a
broadcast the CMS started, and the CMS's name applies while it is asking. On an
X11 session the whole display is sent; on Wayland the renderer page itself
(Chromium screencast). Needs `pip install -r requirements-ndi.txt`; expect
noticeable CPU at 1080p 30 fps on small boards — lower `ndi.fps` there.

**Ubuntu desktop / Wayland.** The player stays on X11 through XWayland and finds
the session's auth file itself at every start, so stock Ubuntu (GNOME on
Wayland, snap Chromium) works. Install with `--autologin` so the screen reaches
its desktop unattended after a reboot:
`sudo bash install.sh --cms https://cms.example --autologin`.

**Live capture.** `show_capture` shows a USB/UVC camera or HDMI grabber full
screen (template capture elements too), through the browser's camera access.
An A/V offset (`av_offset_ms`, ±500 ms, on the command or the capture element)
lines picture and sound up: positive holds the picture back, negative delays
the sound. A new offset for the device already on screen applies without
reopening it; the heartbeat reports `av_offset_ms` and `av_hold_ms`.
DeckLink is not supported on Linux.

**Edge mode.** Control requests and downloads go to the edge server first and
fail over to the cloud after two failures (back when a health probe succeeds);
an edge 404 is confirmed with the cloud before anything is wiped. Proof of play
and player health go to the edge as telemetry.

**Telemetry.** Boot and crash events, renderer crashes (Chromium is relaunched
automatically), wall events, edge failover, proof of play and health (edge
mode), and a machine inventory (OS, remote-access tools, installed apps) daily
or on change. If the CMS refuses an inventory for good, it is sent once a day
until the player version changes. A port the player needs (8081, 8083, 8090)
that another program holds shows in the heartbeat's `error` line ("port
conflict: control API tcp port 8083 is held by nginx"), is reported as a
`port_conflict` event, and is retried every 30 s (`port_conflict_cleared` once
it binds).

**Preview.** When the CMS heartbeat says `preview_watch`, the player pushes
JPEG frames to `/players/{id}/preview-frame` (640×360 every 2 s, or 960×540
every second for a live view).

**Restart from the CMS.** `restart_service` is acknowledged, then the player
exits and systemd (`Restart=always`) starts it again three seconds later.

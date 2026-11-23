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
| mpv 0.33+ with libmpv | Media playback engine |
| X11 or Wayland display server | Must have a display for mpv |

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
`DVSignage/win-web-wrap`) for its CMS contract, at the core tier: everything
except edge/LAN mode, the native interactive-wall engine, machine inventory
and DeckLink capture.

```
main.py                 # entry point: config, logging, crash hook, run
cmx/
├── player.py           # orchestrator: startup, enrollment, heartbeat, SSE,
│                       # commands + acks, playlist download/apply, refresh
├── cms_client.py       # CM:EX REST client (timeouts, secret header, Range resume)
├── commands.py         # command envelope: execute once, ack every delivery
├── items.py            # playlist item classification, dwell, clip in/out
├── cache.py            # {content_id}{ext} cache, byte sniffing, retention, retries
├── playback.py         # mpv wrapper + playlist engine
├── browser.py          # Chromium kiosk for web / RSS / template pages
├── wall.py             # wall crop filters, multicast wall stream
├── screens.py          # registration (code + Connect:EX QR) and status screens
├── telemetry.py        # spooled telemetry queue, boot/crash events
├── faults.py           # the heartbeat "error" line
├── health.py           # metrics, LAN address
├── preview.py          # pushed live preview
├── local_api.py        # local API on port 8081
└── config.py           # config.yaml, paths, state.json
ndi/engine.py           # optional NDI receive/send
tests/                  # pytest suite + fake_cms.py
```

Run the tests with `./run_tests.sh` (or `python3 -m pytest -q`); they need no
mpv, display or browser.

---

## How it works

**Enrollment.** The player requests a code and shows it with a Connect:EX QR
code (`dvsiconnect://enroll?...`). It keeps polling with the same code through
404/429/503 answers and only asks for a new one when the code expires, is
rejected, or nobody approves it for 10 minutes. The approval's `player_secret`
is saved and sent as `X-Player-Secret` on every player request.

**Startup.** `offline_playlist.json` plays from the cache straight away, then
one heartbeat (bounded to 5 s) decides: registered → normal routines; 404 →
re-enroll; unreachable → keep playing offline.

**Heartbeat.** Every 500 ms online, every 15 s while offline. It reports
playback state, clip-relative position/duration, what is on screen, an `error`
line for real faults, system metrics and the LAN address. A 404 means the
player was deleted: identity, cache and offline copy are wiped and a new code
is shown.

**Commands** arrive on the heartbeat and on the SSE stream
(`/players/{id}/events`). A command with a `command_id` runs once and every
delivery is acknowledged (`POST /players/{id}/ack`) with its real result;
commands this player doesn't implement are refused with a reason. Supported:
`load_playlist`, `load_content`, `load_wall_content`, `load_wall_rtp`,
`stop_wall`, `play`, `pause`, `stop`, `replay`/`restart`, `next`, `previous`,
`seek`, `refresh`, `set_volume`, `delete_player`, `restart_service`, and the
NDI commands. Desired state on the heartbeat (`audio_volume`, `audio_muted`,
`desired_blank`) is applied too.

**Content delivery.**

```
load_playlist (new playlist_hash)
  └─ GET /api/v1/players/{id}/assigned-playlist
       ├─ save offline_playlist.json
       ├─ template → Chromium on /api/v1/templates/{id}/render (scaled to fit)
       ├─ url/web  → Chromium on content.url
       ├─ rss      → Chromium on /api/v1/rss/{id}/render
       └─ media    → cache/{content_id}{ext}
            ├─ cached → play
            └─ GET /api/v1/content/{id}/stream (resumes a partial .tmp with Range)
                 ├─ first item ready → start playing
                 ├─ 503 / failure → retried in the background (30 s … 30 min)
                 └─ the cache keeps the last N playlists (cache.keep_playlists)
```

An identical playlist re-sent by the CMS keeps playing where it is instead of
restarting. A single-item playlist loops in place. `clip_start`/`clip_end`
trim videos, enforced by mpv.

**Preview.** When the CMS heartbeat says `preview_watch`, the player pushes
JPEG frames to `/players/{id}/preview-frame` (640×360 every 2 s, or 960×540
every second for a live view).

**Restart from the CMS.** `restart_service` is acknowledged, then the player
exits and systemd (`Restart=always`) starts it again three seconds later.

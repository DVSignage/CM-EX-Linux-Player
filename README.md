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
| `/home/signage/.config/signage-player/state.json` | Player identity (player_id, enrollment state) |
| `/home/signage/.config/signage-player/cache/` | Downloaded media files |
| `/home/signage/.config/signage-player/offline_playlist.json` | Last known playlist for instant boot playback |
| `/etc/systemd/system/signage-player.service` | Systemd service unit |

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

```
player/
├── main.py              # Full orchestrator -- enrollment, heartbeat,
│                        # download, playback, dedup, GC
├── core/
│   └── player.py        # MpvPlayer -- thin mpv wrapper (fullscreen,
│                        # OSD text, EOF callback, play/pause/stop)
├── config.yaml.example  # Template config
├── signage-player.service  # Systemd unit file
├── install.sh           # Automated installer
└── requirements.txt     # Python dependencies
```

`main.py` is intentionally a single self-contained file. All CMS communication, caching, and playback orchestration lives there -- making it easy to read, copy to a device, or modify without a complex module tree.

---

## How content delivery works

```
CMS Portal
  └─ Admin assigns playlist to player

Player heartbeat (every 500ms)
  └─ CMS responds: { command: "load_playlist", playlist_hash: "..." }

Player checks hash against last loaded hash
  ├─ Same → skip (no redundant fetch)
  └─ Different → GET /api/v1/players/{id}/assigned-playlist
       └─ For each item: GET /api/v1/content/{id}/stream
            ├─ Already cached → play immediately
            └─ Not cached → download to .tmp, rename on completion
                 └─ First file ready → start playing
                 └─ Rest download in background → playlist updates seamlessly
```

On reboot, `offline_playlist.json` is loaded instantly from disk before any network request -- so playback resumes in under a second even on a slow connection.

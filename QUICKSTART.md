# Quick Start Guide

Get the Digital Signage Player running in 5 minutes.

## Prerequisites

- Linux system with X11
- Python 3.10+
- mpv installed

## Installation

### 1. Install Dependencies

**Ubuntu/Debian:**
```bash
sudo apt-get install -y python3 python3-pip mpv libmpv-dev
```

**Fedora/RHEL:**
```bash
sudo dnf install -y python3 python3-pip mpv mpv-libs-devel
```

### 2. Install Python Packages

```bash
cd player
pip3 install -r requirements.txt
```

### 3. Create Configuration

```bash
# Generate default config
python3 dev_tools.py generate-config config.yaml

# Edit configuration
nano config.yaml
```

Update these important settings:
- `player.id`: Unique ID for this player
- `player.name`: Display name
- `api.base_url`: URL of your CMS API server

### 4. Create Cache Directory

```bash
mkdir -p /tmp/signage-cache
```

Update `cache.directory` in `config.yaml` to `/tmp/signage-cache`

## Running

### Start the Player

```bash
python3 main.py
```

The player will:
1. Connect to the API server
2. Send heartbeat
3. Wait for playlist assignment
4. Start playback when playlist is received

### Test Without API Server

Create a test playlist:

```bash
python3 dev_tools.py create-test-playlist test_playlist.json
```

Edit `test_playlist.json` to point to real media files on your system, then modify `main.py` to load this local playlist instead of fetching from the API.

## Testing

Run the test suite:

```bash
./run_tests.sh
```

## Troubleshooting

### "Cannot connect to API"

- Verify API server is running
- Check `api.base_url` in configuration
- Test with: `curl http://your-api-server:8000/api/v1/health`

### "No video output"

- Set DISPLAY: `export DISPLAY=:0`
- Check mpv works: `mpv /path/to/test-video.mp4`
- Verify X server is running

### "python-mpv not found"

```bash
pip3 install python-mpv
```

If that fails, install libmpv development files first:
```bash
sudo apt-get install libmpv-dev  # Ubuntu/Debian
sudo dnf install mpv-libs-devel  # Fedora/RHEL
```

## Next Steps

- Configure systemd service for automatic startup
- Set up network share for content
- Configure playlist in web portal
- Assign playlist to player

See [README.md](README.md) for complete documentation.

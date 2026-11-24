# Quick Start Guide

Run the CM:EX Linux Player from a clone, for development or a quick test.
For a real screen, use the installer in [README.md](README.md).

## Prerequisites

- Linux with X11 (or Wayland with XWayland)
- Python 3.10+
- mpv with libmpv; Chromium for templates, web pages and RSS

```bash
sudo apt-get install -y python3 python3-venv mpv libmpv2 chromium   # Ubuntu/Debian
```

## Install

```bash
git clone https://github.com/DVSignage/CM-EX-Linux-Player.git
cd CM-EX-Linux-Player
python3 -m venv venv
venv/bin/pip install -r requirements-dev.txt
venv/bin/pip install -r requirements-ndi.txt   # optional NDI support
```

## Configure

```bash
cp config.yaml.example config.yaml
nano config.yaml        # set cms_url
```

`cms_url` is the only required setting. The player registers itself: on first
start it shows a registration code (and a Connect:EX QR code) to approve in
CM:EX under Players.

## Run

```bash
venv/bin/python main.py
```

State lives in `~/.config/signage-player/` (identity, cache, offline playlist).
Delete `state.json` there to register the screen again.

## Try it without a CMS

`tests/fake_cms.py` serves the CM:EX player contract with a folder of media
files as the playlist:

```bash
venv/bin/python tests/fake_cms.py --port 8000 --media ~/Videos/test &
# config.yaml: cms_url: "http://127.0.0.1:8000"
venv/bin/python main.py
curl -X POST http://127.0.0.1:8000/_admin/approve          # approve the code
curl -X POST http://127.0.0.1:8000/_admin/command \
     -d '{"command":"seek","command_id":"k1","command_args":{"position":5}}'
curl http://127.0.0.1:8000/_admin/acks                     # what the player acknowledged
```

## Test

```bash
./run_tests.sh
```

The suite needs no mpv, display or browser.

## Troubleshooting

- **No video output:** check `DISPLAY` (`echo $DISPLAY`) and that `mpv some-video.mp4` works.
- **`python-mpv` cannot find libmpv:** install `libmpv2` (Debian/Ubuntu) or `mpv-libs` (Fedora), then `sudo ldconfig`.
- **Templates or web pages don't show:** install Chromium (`chromium` or `chromium-browser`).
- **Logs:** the player logs to stdout; under systemd use `journalctl -u signage-player -f`.

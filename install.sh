#!/usr/bin/env bash
# ============================================================================
# CM:EX Linux Player — Installer
#
# Usage:
#   sudo bash install.sh                          # interactive
#   sudo bash install.sh --cms http://10.0.0.5:8000  # non-interactive
#   sudo bash install.sh --cms http://10.0.0.5:8000 --user dvsi
#
# Fleet deployment (same command on every machine):
#   git clone https://github.com/DVSignage/CM-EX-Linux-Player.git /tmp/cmx-player
#   sudo bash /tmp/cmx-player/install.sh --cms http://cms.local:8000
#
# Safe to run multiple times (idempotent). Re-running updates player files
# and pip packages without losing your config.yaml or enrollment state.
# ============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Parse command-line arguments
# ---------------------------------------------------------------------------
ARG_CMS_URL=""
ARG_USER=""
ARG_AUTOLOGIN=0
WAYLAND_SESSION=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cms|--cms-url)
            ARG_CMS_URL="$2"; shift 2 ;;
        --user)
            ARG_USER="$2"; shift 2 ;;
        --autologin)
            ARG_AUTOLOGIN=1; shift ;;
        --help|-h)
            echo "Usage: sudo bash install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --cms URL    CMS server URL (e.g. http://10.0.0.5:8000)"
            echo "  --user NAME  Desktop user (auto-detected if omitted)"
            echo "  --autologin  Log the desktop user in automatically at boot (GDM / LightDM)"
            echo "  --help       Show this help"
            echo ""
            echo "Examples:"
            echo "  sudo bash install.sh                              # interactive"
            echo "  sudo bash install.sh --cms http://cms:8000        # fleet deploy"
            echo "  sudo bash install.sh --cms http://cms:8000 --user dvsi"
            exit 0 ;;
        *)
            echo "Unknown option: $1 (try --help)"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Colours and helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Colour

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()     { echo -e "${RED}[ERROR]${NC} $*"; }
step()    { echo -e "\n${CYAN}${BOLD}▶ $*${NC}"; }
divider() { echo -e "${CYAN}────────────────────────────────────────────────────${NC}"; }

fail() {
    err "$*"
    exit 1
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
step "Pre-flight checks"

if [[ $EUID -ne 0 ]]; then
    fail "This script must be run as root.  Try: sudo bash install.sh"
fi

# ---------------------------------------------------------------------------
# Auto-detect the desktop user (the user logged into the physical display)
# ---------------------------------------------------------------------------
DESKTOP_USER=""
DESKTOP_UID=""
XAUTH_PATH=""
X_DISPLAY=":0"

detect_desktop_user() {
    # If user was specified via --user, use that directly
    if [[ -n "$ARG_USER" ]]; then
        DESKTOP_USER="$ARG_USER"
        info "Using user from --user flag: $DESKTOP_USER"
    fi

    # Method 1: Find who owns the running Xorg process
    if [[ -z "$DESKTOP_USER" ]]; then
        local xorg_user
        xorg_user=$(ps -eo user,comm | grep -i '[Xx]org' | head -1 | awk '{print $1}')
        if [[ -n "$xorg_user" && "$xorg_user" != "root" ]]; then
            DESKTOP_USER="$xorg_user"
        fi
    fi

    # Method 2: Check who is logged into a graphical session
    if [[ -z "$DESKTOP_USER" ]] && command -v loginctl &>/dev/null; then
        local session_user
        session_user=$(loginctl list-sessions --no-legend 2>/dev/null | while read -r sid uid user seat _rest; do
            stype=$(loginctl show-session "$sid" -p Type --value 2>/dev/null || echo "")
            if [[ "$stype" == "x11" || "$stype" == "wayland" ]]; then
                echo "$user"
                break
            fi
        done)
        [[ -n "$session_user" ]] && DESKTOP_USER="$session_user"
    fi

    # Method 3: Check who owns tty1/tty2
    if [[ -z "$DESKTOP_USER" ]]; then
        local tty_user
        tty_user=$(who | grep -E 'tty[12]' | head -1 | awk '{print $1}')
        [[ -n "$tty_user" ]] && DESKTOP_USER="$tty_user"
    fi

    if [[ -z "$DESKTOP_USER" ]]; then
        warn "Could not auto-detect desktop user."
        if [[ -t 0 ]]; then
            read -rp "  Enter the username logged into the desktop: " DESKTOP_USER
        fi
        [[ -z "$DESKTOP_USER" ]] && fail "No desktop user specified. Cannot continue."
    fi

    # Get UID
    DESKTOP_UID=$(id -u "$DESKTOP_USER" 2>/dev/null) || fail "User '$DESKTOP_USER' does not exist"

    ok "Desktop user: ${BOLD}$DESKTOP_USER${NC} (UID $DESKTOP_UID)"
}

detect_desktop_user

# ---------------------------------------------------------------------------
# Auto-detect X11 display and Xauthority
# ---------------------------------------------------------------------------
detect_display() {
    # GNOME/KDE on Wayland: the player uses XWayland, whose auth file
    # (/run/user/<uid>/.mutter-Xwaylandauth.*) gets a new random name at every
    # login — so it is NOT written into the service; the player finds the live
    # one when it starts (cmx/display.py).
    if ps -eo args= | grep -q '[X]wayland' || ls /run/user/"$DESKTOP_UID"/wayland-* &>/dev/null; then
        WAYLAND_SESSION=1
        local xw_cmd
        xw_cmd=$(ps -eo args= | grep '[X]wayland' | head -1)
        if echo "$xw_cmd" | grep -qE ' :[0-9]+'; then
            X_DISPLAY=$(echo "$xw_cmd" | grep -oE ' :[0-9]+' | head -1 | tr -d ' ')
        fi
        XAUTH_PATH=""
        ok "Wayland session — display ${BOLD}$X_DISPLAY${NC} through XWayland (the player finds its auth file at start)"
        return
    fi

    # X11. Read the Xorg arguments ONLY: `ps aux` also prints the start time
    # (10:37), which a ':[0-9]+' match took for the display (DISPLAY=:37).
    local xorg_cmd
    xorg_cmd=$(ps -eo args= | grep -E '^[^ ]*[Xx]org( |$)' | head -1)
    local d
    d=$(echo "$xorg_cmd" | grep -oE '(^| ):[0-9]+( |$)' | head -1 | tr -d ' ')
    [[ -n "$d" ]] && X_DISPLAY="$d"

    # An Xauthority the PLAYER can use: the service runs as $DESKTOP_USER, so the
    # X server's own -auth file (lightdm's /var/run/lightdm/root/:0 is root-only)
    # only counts if that user can read it; the user's own file usually works.
    local home_dir
    home_dir=$(getent passwd "$DESKTOP_USER" | cut -d: -f6)
    local candidates=()
    if echo "$xorg_cmd" | grep -q -- ' -auth '; then
        candidates+=("$(echo "$xorg_cmd" | sed 's/.* -auth \([^ ]*\).*/\1/')")
    fi
    candidates+=("/run/user/$DESKTOP_UID/gdm/Xauthority" "${home_dir:-/home/$DESKTOP_USER}/.Xauthority"
                 "/run/user/$DESKTOP_UID/.Xauthority")

    # Checked AS the desktop user, the way the service will open the display.
    as_user_can_open() {  # $1 = Xauthority path, or "" for none
        local probe=""
        if command -v xdpyinfo &>/dev/null; then probe="xdpyinfo"
        elif command -v xset &>/dev/null; then probe="xset q"
        else return 2; fi
        runuser -u "$DESKTOP_USER" -- env DISPLAY="$X_DISPLAY" ${1:+XAUTHORITY="$1"} $probe &>/dev/null
    }

    XAUTH_PATH=""
    local c rc
    for c in "${candidates[@]}"; do
        [[ -n "$c" && -f "$c" ]] || continue
        runuser -u "$DESKTOP_USER" -- test -r "$c" || continue
        as_user_can_open "$c"; rc=$?
        if [[ $rc -eq 0 ]]; then
            XAUTH_PATH="$c"
            ok "Display: ${BOLD}$X_DISPLAY${NC}  Xauthority: ${BOLD}$XAUTH_PATH${NC}"
            return
        elif [[ $rc -eq 2 ]]; then   # nothing to test with: take the first readable file
            XAUTH_PATH="$c"
            ok "Display: ${BOLD}$X_DISPLAY${NC}  Xauthority: ${BOLD}$XAUTH_PATH${NC} (not verified: no xdpyinfo/xset)"
            return
        fi
    done

    # Some setups need no Xauthority at all
    if as_user_can_open ""; then
        ok "Display: ${BOLD}$X_DISPLAY${NC}  (no explicit Xauthority needed)"
        return
    fi

    warn "Could not verify display access for $DESKTOP_USER on $X_DISPLAY. The service may need manual configuration."
    warn "See: sudo nano /etc/systemd/system/signage-player.service"
}

detect_display

# ---------------------------------------------------------------------------
# Resolve source files
# ---------------------------------------------------------------------------
step "Resolving source files"

SOURCE_DIR=""
TEMP_DIR=""

resolve_source() {
    if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "bash" && -f "${BASH_SOURCE[0]}" ]]; then
        SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        if [[ -f "$SOURCE_DIR/main.py" && -f "$SOURCE_DIR/requirements.txt" ]]; then
            info "Source files found at ${BOLD}$SOURCE_DIR${NC}"
            return
        fi
    fi

    info "Downloading player files from GitHub..."
    TEMP_DIR="$(mktemp -d)"
    if command -v git &>/dev/null; then
        git clone --depth 1 https://github.com/DVSignage/CM-EX-Linux-Player.git "$TEMP_DIR" \
            || fail "git clone failed. Check your network connection."
    elif command -v wget &>/dev/null; then
        wget -qO- https://github.com/DVSignage/CM-EX-Linux-Player/archive/refs/heads/main.tar.gz \
            | tar xz --strip-components=1 -C "$TEMP_DIR" \
            || fail "Download failed. Check your network connection."
    elif command -v curl &>/dev/null; then
        curl -sSL https://github.com/DVSignage/CM-EX-Linux-Player/archive/refs/heads/main.tar.gz \
            | tar xz --strip-components=1 -C "$TEMP_DIR" \
            || fail "Download failed. Check your network connection."
    else
        fail "Neither git, curl, nor wget found. Install one and try again."
    fi
    SOURCE_DIR="$TEMP_DIR"
    ok "Downloaded to $TEMP_DIR"
}

resolve_source

for f in main.py requirements.txt signage-player.service config.yaml.example; do
    [[ -f "$SOURCE_DIR/$f" ]] || fail "Missing required file: $SOURCE_DIR/$f"
done
ok "All required source files present"

# ---------------------------------------------------------------------------
# Detect distro
# ---------------------------------------------------------------------------
step "Detecting Linux distribution"

DISTRO="unknown"

detect_distro() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        case "${ID:-}" in
            ubuntu|debian|linuxmint|pop|elementary|zorin|raspbian)
                DISTRO="debian" ;;
            fedora|rhel|centos|rocky|alma|ol)
                DISTRO="fedora" ;;
            arch|manjaro|endeavouros|garuda)
                DISTRO="arch" ;;
            opensuse*|sles)
                DISTRO="suse" ;;
        esac
    fi

    if [[ "$DISTRO" == "unknown" ]]; then
        if command -v apt-get &>/dev/null; then
            DISTRO="debian"
        elif command -v dnf &>/dev/null || command -v yum &>/dev/null; then
            DISTRO="fedora"
        elif command -v pacman &>/dev/null; then
            DISTRO="arch"
        fi
    fi
}

detect_distro

case "$DISTRO" in
    debian) ok "Detected Debian/Ubuntu family" ;;
    fedora) ok "Detected Fedora/RHEL family" ;;
    arch)   ok "Detected Arch Linux family" ;;
    suse)   ok "Detected openSUSE/SLES family" ;;
    *)      fail "Unsupported distribution. Install dependencies manually and re-run." ;;
esac

# ---------------------------------------------------------------------------
# Install system dependencies
# ---------------------------------------------------------------------------
step "Installing system dependencies"

install_deps_debian() {
    apt-get update -qq
    # Install packages one-by-one so a held/broken package doesn't block the rest.
    # mpv and libmpv-dev may already be installed (and held back by PPA conflicts).
    local pkgs=(python3 python3-pip python3-venv mpv libmpv-dev avahi-daemon libavahi-client-dev curl libgtk2.0-0 fonts-dejavu-core)
    # Chromium draws templates, walls and the player's screens (the renderer
    # page). Debian ships "chromium"; Ubuntu's "chromium-browser" is a snap.
    if ! command -v chromium &>/dev/null && ! command -v chromium-browser &>/dev/null; then
        apt-get install -y -qq chromium 2>/dev/null || apt-get install -y -qq chromium-browser 2>/dev/null \
            || warn "Chromium could not be installed — the player will run in mpv-only mode (no native templates or walls)"
    fi
    local failed=()
    for pkg in "${pkgs[@]}"; do
        if dpkg -s "$pkg" &>/dev/null; then
            info "$pkg is already installed — skipping"
        else
            if ! apt-get install -y -qq "$pkg" 2>/dev/null; then
                warn "Could not install $pkg — will try to continue without it"
                failed+=("$pkg")
            fi
        fi
    done
    # Hard-fail only if critical packages are missing
    for critical in python3 python3-pip python3-venv curl; do
        if ! command -v "${critical/python3-pip/pip3}" &>/dev/null && ! dpkg -s "$critical" &>/dev/null; then
            fail "Critical package '$critical' could not be installed. Fix manually: sudo apt install $critical"
        fi
    done
    # mpv is required but may already be present
    if ! command -v mpv &>/dev/null; then
        fail "mpv is not installed and could not be installed. Fix: sudo apt install mpv"
    fi
    if [[ ${#failed[@]} -gt 0 ]]; then
        warn "Some packages failed to install: ${failed[*]} — continuing with what's available"
    fi
}

install_deps_fedora() {
    if command -v dnf &>/dev/null; then
        dnf install -y python3 python3-pip python3-virtualenv mpv mpv-libs-devel \
            avahi avahi-devel curl dejavu-sans-fonts
        dnf install -y chromium || warn "Chromium could not be installed — mpv-only mode"
    else
        yum install -y python3 python3-pip python3-virtualenv mpv mpv-libs-devel \
            avahi avahi-devel curl
        yum install -y chromium || warn "Chromium could not be installed — mpv-only mode"
    fi
}

install_deps_arch() {
    pacman -Syu --noconfirm --needed python python-pip mpv avahi curl ttf-dejavu
    pacman -S --noconfirm --needed chromium || warn "Chromium could not be installed — mpv-only mode"
}

install_deps_suse() {
    zypper install -y python3 python3-pip python3-virtualenv mpv libmpv-devel \
        avahi avahi-utils curl dejavu-fonts
    zypper install -y chromium || warn "Chromium could not be installed — mpv-only mode"
}

case "$DISTRO" in
    debian) install_deps_debian ;;
    fedora) install_deps_fedora ;;
    arch)   install_deps_arch ;;
    suse)   install_deps_suse ;;
esac

ok "System dependencies installed"

# ---------------------------------------------------------------------------
# Copy player files
# ---------------------------------------------------------------------------
INSTALL_DIR="/opt/signage-player"

step "Setting up ${BOLD}$INSTALL_DIR${NC}"

mkdir -p "$INSTALL_DIR"

if command -v rsync &>/dev/null; then
    rsync -a --delete \
        --exclude='venv/' \
        --exclude='__pycache__/' \
        --exclude='.git/' \
        --exclude='.gitignore' \
        --exclude='config.yaml' \
        --exclude='*.pyc' \
        --exclude='tests/' \
        --exclude='install.sh' \
        "$SOURCE_DIR/" "$INSTALL_DIR/"
else
    find "$INSTALL_DIR" -name '*.py' -not -path '*/venv/*' -delete 2>/dev/null || true
    cp -a "$SOURCE_DIR/." "$INSTALL_DIR/"
    rm -rf "$INSTALL_DIR/venv" "$INSTALL_DIR/__pycache__" "$INSTALL_DIR/.git" \
           "$INSTALL_DIR/.gitignore" "$INSTALL_DIR/tests" "$INSTALL_DIR/install.sh" 2>/dev/null || true
fi

ok "Player files copied to $INSTALL_DIR"

# ---------------------------------------------------------------------------
# Python virtual environment + correct python-mpv version
# ---------------------------------------------------------------------------
step "Creating Python virtual environment"

VENV_DIR="$INSTALL_DIR/venv"

python3 -m venv "$VENV_DIR" --clear
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet

# NDI is optional: ndi-python has no wheel for every Python version, so a
# failure here must not abort the whole install.
if [[ -f "$INSTALL_DIR/requirements-ndi.txt" ]]; then
    if "$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements-ndi.txt" --quiet; then
        ok "NDI support installed"
    else
        warn "NDI packages could not be installed — the player will run without NDI"
    fi
fi

# Check if python-mpv is compatible with system libmpv
# If not, downgrade to a version that works
if ! "$VENV_DIR/bin/python3" -c "import mpv; mpv.MPV()" &>/dev/null 2>&1; then
    warn "python-mpv latest is incompatible with system libmpv — installing compatible version"
    "$VENV_DIR/bin/pip" install python-mpv==0.5.2 --quiet
    if "$VENV_DIR/bin/python3" -c "import mpv" &>/dev/null 2>&1; then
        ok "python-mpv 0.5.2 installed (compatible with system libmpv)"
    else
        warn "python-mpv still failing — mpv playback may not work. Consider updating system mpv."
    fi
else
    ok "python-mpv is compatible with system libmpv"
fi

ok "Virtual environment ready at $VENV_DIR"

# ---------------------------------------------------------------------------
# Set ownership to desktop user (not a separate signage user)
# ---------------------------------------------------------------------------
step "Setting file ownership"

chown -R "$DESKTOP_USER:$DESKTOP_USER" "$INSTALL_DIR"

# Create the state directory under the desktop user's home
STATE_DIR="/home/$DESKTOP_USER/.config/signage-player"
mkdir -p "$STATE_DIR"
chown -R "$DESKTOP_USER:$DESKTOP_USER" "$STATE_DIR"
ok "State directory ready at $STATE_DIR"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
step "Checking configuration"

CONFIG_FILE="$INSTALL_DIR/config.yaml"

write_full_config() {
    # Write a complete, known-good config.yaml — no sed substitution needed
    local url="$1"
    cat > "$CONFIG_FILE" <<CFGEOF
cms_url: "$url"

playback:
  display: "$X_DISPLAY"
  audio_output: "auto"
  default_image_duration: 10

log_level: "INFO"
CFGEOF
    chown "$DESKTOP_USER:$DESKTOP_USER" "$CONFIG_FILE"
}

if [[ -f "$CONFIG_FILE" ]]; then
    if [[ -n "$ARG_CMS_URL" ]]; then
        # --cms flag: rewrite config with the provided URL (safe overwrite)
        write_full_config "$ARG_CMS_URL"
        ok "config.yaml rewritten with cms_url=$ARG_CMS_URL (via --cms flag)"
    else
        ok "config.yaml already exists — preserving current configuration"
    fi
else
    if [[ -n "$ARG_CMS_URL" ]]; then
        # Fleet deploy: write config directly
        write_full_config "$ARG_CMS_URL"
        ok "config.yaml created with cms_url=$ARG_CMS_URL (via --cms flag)"
    elif [[ -t 0 ]]; then
        # Priority 2: Interactive prompt
        echo ""
        echo -e "  ${BOLD}You must set the CMS URL.${NC}"
        echo ""
        echo -e "  Enter your CMS server address, e.g.: ${CYAN}http://192.168.1.100:8000${NC}"
        echo ""
        read -rp "  CMS URL: " CMS_URL
        if [[ -n "$CMS_URL" ]]; then
            CMS_URL="${CMS_URL%\"}"
            CMS_URL="${CMS_URL#\"}"
            write_full_config "$CMS_URL"
            ok "config.yaml created with cms_url=$CMS_URL"
        else
            cp "$INSTALL_DIR/config.yaml.example" "$CONFIG_FILE"
            chown "$DESKTOP_USER:$DESKTOP_USER" "$CONFIG_FILE"
            warn "Skipped — remember to edit $CONFIG_FILE before starting"
        fi
    else
        cp "$INSTALL_DIR/config.yaml.example" "$CONFIG_FILE"
        chown "$DESKTOP_USER:$DESKTOP_USER" "$CONFIG_FILE"
        warn "Non-interactive mode — edit $CONFIG_FILE or re-run with --cms URL"
    fi

    # Test connectivity (regardless of how URL was set)
    CONFIGURED_URL=$(grep 'cms_url:' "$CONFIG_FILE" | sed 's/.*cms_url:\s*"\?\([^"]*\)"\?/\1/' | tr -d '[:space:]')
    if [[ -n "$CONFIGURED_URL" && "$CONFIGURED_URL" != *"example"* ]]; then
        echo ""
        info "Testing connection to CMS..."
        if curl -sf --connect-timeout 5 "${CONFIGURED_URL}/api/v1/health" &>/dev/null; then
            ok "CMS is reachable at $CONFIGURED_URL"
        else
            warn "Could not reach CMS at ${CONFIGURED_URL}/api/v1/health"
            warn "Check the URL/IP and make sure the CMS server is running."
            warn "The player will keep retrying automatically."
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Systemd service (runs as desktop user with display access)
# ---------------------------------------------------------------------------
step "Installing systemd service"

SYSTEMD_FILE="/etc/systemd/system/signage-player.service"

cat > "$SYSTEMD_FILE" << SERVICEEOF
[Unit]
Description=Digital Signage Player Daemon
# Started with the desktop (graphical.target, below), after the display manager.
# NOT After=graphical.target: graphical.target waits for the units it wants, so
# that is an ordering cycle and systemd drops the player's start at every boot.
After=network-online.target display-manager.service
Wants=network-online.target

[Service]
Type=simple
User=$DESKTOP_USER
Group=$DESKTOP_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python3 $INSTALL_DIR/main.py
Restart=always
# restart_service from the CMS exits the process; systemd brings it back
RestartSec=3
StandardOutput=journal
StandardError=journal

# Display access
Environment="DISPLAY=$X_DISPLAY"
Environment="XDG_RUNTIME_DIR=/run/user/$DESKTOP_UID"
Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$DESKTOP_UID/bus"
$([ -n "$XAUTH_PATH" ] && echo "Environment=\"XAUTHORITY=$XAUTH_PATH\"")

# Security
NoNewPrivileges=true
PrivateTmp=true
# ...but the X server's sockets live in /tmp/.X11-unix: share that one folder, or
# the player sees no display and waits for a desktop session that never comes.
BindPaths=-/tmp/.X11-unix

[Install]
WantedBy=graphical.target
SERVICEEOF

systemctl daemon-reload
ok "Service file installed at $SYSTEMD_FILE"

# ---------------------------------------------------------------------------
# Automatic login (opt-in): an unattended screen must reach its desktop after a reboot
# ---------------------------------------------------------------------------
if [[ "$ARG_AUTOLOGIN" == "1" ]]; then
    step "Enabling automatic login for $DESKTOP_USER"
    if [[ -f /etc/gdm3/custom.conf ]] || [[ -d /etc/gdm3 ]]; then
        GDM_CONF=/etc/gdm3/custom.conf
        [[ -f "$GDM_CONF" ]] || printf '[daemon]\n' > "$GDM_CONF"
        cp -n "$GDM_CONF" "$GDM_CONF.cmx-backup" 2>/dev/null || true
        grep -q '^\[daemon\]' "$GDM_CONF" || printf '\n[daemon]\n' >> "$GDM_CONF"
        sed -i '/^AutomaticLoginEnable *=/d; /^AutomaticLogin *=/d' "$GDM_CONF"
        sed -i "/^\[daemon\]/a AutomaticLogin=$DESKTOP_USER" "$GDM_CONF"
        sed -i "/^\[daemon\]/a AutomaticLoginEnable=true" "$GDM_CONF"
        ok "GDM logs $DESKTOP_USER in automatically (backup: $GDM_CONF.cmx-backup)"
    elif [[ -d /etc/lightdm ]]; then
        mkdir -p /etc/lightdm/lightdm.conf.d
        printf '[Seat:*]\nautologin-user=%s\nautologin-user-timeout=0\n' "$DESKTOP_USER" \
            > /etc/lightdm/lightdm.conf.d/50-cmx-autologin.conf
        ok "LightDM logs $DESKTOP_USER in automatically"
    else
        warn "No GDM or LightDM found — set up automatic login for $DESKTOP_USER manually"
    fi
fi

# ---------------------------------------------------------------------------
# Enable and start
# ---------------------------------------------------------------------------
step "Configuring firewall"

# Open port 8081 (local player API — NDI sources, preview, device info)
if command -v ufw &>/dev/null; then
    if ufw status | grep -q "Status: active"; then
        ufw allow 8081/tcp &>/dev/null && ok "ufw: opened port 8081 (player API)"
        ufw allow 8083/tcp &>/dev/null && ok "ufw: opened port 8083 (control API, LAN triggers when enabled)"
    else
        # ufw is inactive — enable it and open required ports
        ufw allow ssh &>/dev/null        # keep SSH open!
        ufw allow 8081/tcp &>/dev/null
        ufw allow 8083/tcp &>/dev/null
        ufw --force enable &>/dev/null
        ok "ufw: enabled and opened port 8081 (player API)"
    fi
else
    info "ufw not found — skipping firewall config (ensure port 8081 is accessible)"
fi

step "Enabling and starting service"

systemctl reenable signage-player   # reenable: moves an older multi-user.target link to graphical.target
systemctl restart signage-player
ok "signage-player service enabled and started"

# ---------------------------------------------------------------------------
# Final status
# ---------------------------------------------------------------------------
divider
step "Installation complete"
divider
echo ""
systemctl status signage-player --no-pager --lines=5 || true
echo ""
divider
echo ""
echo -e "${GREEN}${BOLD}CM:EX Linux Player is installed and running.${NC}"
echo ""
echo -e "  Desktop user:  ${BOLD}$DESKTOP_USER${NC}"
echo -e "  Display:       ${BOLD}$X_DISPLAY${NC}"
echo -e "  Config file:   ${BOLD}$CONFIG_FILE${NC}"
echo -e "  Install dir:   ${BOLD}$INSTALL_DIR${NC}"
echo -e "  Logs:          ${CYAN}sudo journalctl -u signage-player -f${NC}"
echo ""
echo -e "  To reconfigure:  ${CYAN}sudo nano $CONFIG_FILE && sudo systemctl restart signage-player${NC}"
echo ""

# Show fleet deployment hint
CONFIGURED_URL=$(grep 'cms_url:' "$CONFIG_FILE" 2>/dev/null | sed 's/.*cms_url:\s*"\?\([^"]*\)"\?/\1/' | tr -d '[:space:]')
if [[ -n "$CONFIGURED_URL" ]]; then
    echo -e "  ${BOLD}Deploy to other machines with the same config:${NC}"
    echo -e "  ${CYAN}git clone https://github.com/DVSignage/CM-EX-Linux-Player.git /tmp/cmx-player && sudo bash /tmp/cmx-player/install.sh --cms \"$CONFIGURED_URL\" --user $DESKTOP_USER${NC}"
    echo ""
fi

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf "$TEMP_DIR"
fi

exit 0

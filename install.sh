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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cms|--cms-url)
            ARG_CMS_URL="$2"; shift 2 ;;
        --user)
            ARG_USER="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: sudo bash install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --cms URL    CMS server URL (e.g. http://10.0.0.5:8000)"
            echo "  --user NAME  Desktop user (auto-detected if omitted)"
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
    # Find XAUTHORITY from Xorg process command line
    local xorg_cmd
    xorg_cmd=$(ps aux | grep -i '[Xx]org' | head -1)

    # Extract -auth argument
    if echo "$xorg_cmd" | grep -q '\-auth'; then
        XAUTH_PATH=$(echo "$xorg_cmd" | sed 's/.*-auth \([^ ]*\).*/\1/')
    fi

    # Fallback: check common locations
    if [[ -z "$XAUTH_PATH" || ! -f "$XAUTH_PATH" ]]; then
        for candidate in \
            "/run/user/$DESKTOP_UID/gdm/Xauthority" \
            "/home/$DESKTOP_USER/.Xauthority" \
            "/run/user/$DESKTOP_UID/.Xauthority" \
            "/tmp/.X11-unix/../.Xauthority"; do
            if [[ -f "$candidate" ]]; then
                XAUTH_PATH="$candidate"
                break
            fi
        done
    fi

    # Detect display number from Xorg args
    if echo "$xorg_cmd" | grep -qE ':[0-9]+'; then
        X_DISPLAY=$(echo "$xorg_cmd" | grep -oE ':[0-9]+' | head -1)
    fi

    # Verify display works
    if [[ -n "$XAUTH_PATH" ]]; then
        if DISPLAY="$X_DISPLAY" XAUTHORITY="$XAUTH_PATH" xdpyinfo &>/dev/null; then
            ok "Display: ${BOLD}$X_DISPLAY${NC}  Xauthority: ${BOLD}$XAUTH_PATH${NC}"
            return
        fi
    fi

    # Try without explicit XAUTHORITY (some setups work with just DISPLAY)
    if su - "$DESKTOP_USER" -c "DISPLAY=$X_DISPLAY xdpyinfo" &>/dev/null 2>&1; then
        ok "Display: ${BOLD}$X_DISPLAY${NC}  (no explicit Xauthority needed)"
        XAUTH_PATH=""
        return
    fi

    warn "Could not verify display access. The service may need manual configuration."
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
    local pkgs=(python3 python3-pip python3-venv mpv libmpv-dev avahi-daemon libavahi-client-dev curl)
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
            avahi avahi-devel curl
    else
        yum install -y python3 python3-pip python3-virtualenv mpv mpv-libs-devel \
            avahi avahi-devel curl
    fi
}

install_deps_arch() {
    pacman -Syu --noconfirm --needed python python-pip mpv avahi curl
}

install_deps_suse() {
    zypper install -y python3 python3-pip python3-virtualenv mpv libmpv-devel \
        avahi avahi-utils curl
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
After=network-online.target graphical.target
Wants=network-online.target

[Service]
Type=simple
User=$DESKTOP_USER
Group=$DESKTOP_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python3 $INSTALL_DIR/main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Display access
Environment="DISPLAY=$X_DISPLAY"
Environment="XDG_RUNTIME_DIR=/run/user/$DESKTOP_UID"
$([ -n "$XAUTH_PATH" ] && echo "Environment=\"XAUTHORITY=$XAUTH_PATH\"")

# Security
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
ok "Service file installed at $SYSTEMD_FILE"

# ---------------------------------------------------------------------------
# Enable and start
# ---------------------------------------------------------------------------
step "Enabling and starting service"

systemctl enable signage-player
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

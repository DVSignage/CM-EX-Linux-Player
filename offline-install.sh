#!/usr/bin/env bash
# ============================================================================
# CM:EX Linux Player — Offline Installer
#
# This script is embedded inside cmx-player-offline-*.run
# Do not run this directly — use the .run file.
#
# Usage (via .run):
#   sudo bash cmx-player-offline-*.run
#   sudo bash cmx-player-offline-*.run --cms http://10.0.0.5:8080
#   sudo bash cmx-player-offline-*.run --cms http://10.0.0.5:8080 --user dvsi
# ============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Source dir is wherever this script was extracted to
# ---------------------------------------------------------------------------
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
ARG_CMS_URL=""
ARG_USER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cms|--cms-url) ARG_CMS_URL="$2"; shift 2 ;;
        --user)          ARG_USER="$2";    shift 2 ;;
        --help|-h)
            echo "Usage: sudo bash cmx-player-offline-*.run [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --cms URL    CMS server URL (e.g. http://192.168.1.100:8080)"
            echo "  --user NAME  Desktop user (auto-detected if omitted)"
            echo "  --help       Show this help"
            exit 0 ;;
        *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Colours and helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()     { echo -e "${RED}[ERROR]${NC} $*"; }
step()    { echo -e "\n${CYAN}${BOLD}▶ $*${NC}"; }
divider() { echo -e "${CYAN}────────────────────────────────────────────────────${NC}"; }
fail()    { err "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
step "Pre-flight checks"

[[ $EUID -eq 0 ]] || fail "Must run as root.  Try: sudo bash $0"

# Verify bundled assets
[[ -d "$SOURCE_DIR/debs"   ]] || fail "Bundled packages missing (debs/).  Re-download the .run file."
[[ -d "$SOURCE_DIR/wheels" ]] || fail "Bundled wheels missing (wheels/).  Re-download the .run file."
[[ -f "$SOURCE_DIR/main.py" ]] || fail "main.py missing from package."

DEB_COUNT=$(ls "$SOURCE_DIR/debs/"*.deb 2>/dev/null | wc -l)
WHEEL_COUNT=$(ls "$SOURCE_DIR/wheels/"*.whl 2>/dev/null | wc -l)
ok "Package verified: ${DEB_COUNT} system packages, ${WHEEL_COUNT} Python wheels"

# ---------------------------------------------------------------------------
# Detect desktop user
# ---------------------------------------------------------------------------
DESKTOP_USER=""
DESKTOP_UID=""
XAUTH_PATH=""
X_DISPLAY=":0"

step "Detecting desktop user"

detect_desktop_user() {
    if [[ -n "$ARG_USER" ]]; then
        DESKTOP_USER="$ARG_USER"
        info "Using --user flag: $DESKTOP_USER"
        DESKTOP_UID=$(id -u "$DESKTOP_USER" 2>/dev/null) || fail "User '$DESKTOP_USER' does not exist"
        ok "Desktop user: ${BOLD}$DESKTOP_USER${NC} (UID $DESKTOP_UID)"
        return
    fi

    # Method 1: Who owns the Xorg process
    local xorg_user
    xorg_user=$(ps -eo user,comm 2>/dev/null | grep -i '[Xx]org' | head -1 | awk '{print $1}')
    [[ -n "$xorg_user" && "$xorg_user" != "root" ]] && DESKTOP_USER="$xorg_user"

    # Method 2: loginctl graphical session
    if [[ -z "$DESKTOP_USER" ]] && command -v loginctl &>/dev/null; then
        local session_user
        session_user=$(loginctl list-sessions --no-legend 2>/dev/null | while read -r sid _uid user _seat _rest; do
            stype=$(loginctl show-session "$sid" -p Type --value 2>/dev/null || echo "")
            if [[ "$stype" == "x11" || "$stype" == "wayland" ]]; then
                echo "$user"; break
            fi
        done)
        [[ -n "${session_user:-}" ]] && DESKTOP_USER="$session_user"
    fi

    # Method 3: tty owner
    if [[ -z "$DESKTOP_USER" ]]; then
        local tty_user
        tty_user=$(who 2>/dev/null | grep -E 'tty[12]' | head -1 | awk '{print $1}')
        [[ -n "${tty_user:-}" ]] && DESKTOP_USER="$tty_user"
    fi

    if [[ -z "$DESKTOP_USER" ]]; then
        warn "Could not auto-detect desktop user."
        if [[ -t 0 ]]; then
            read -rp "  Enter the username logged into the desktop: " DESKTOP_USER
        fi
        [[ -z "$DESKTOP_USER" ]] && fail "No desktop user specified."
    fi

    DESKTOP_UID=$(id -u "$DESKTOP_USER" 2>/dev/null) || fail "User '$DESKTOP_USER' does not exist"
    ok "Desktop user: ${BOLD}$DESKTOP_USER${NC} (UID $DESKTOP_UID)"
}

detect_desktop_user

# ---------------------------------------------------------------------------
# Detect X11 display
# ---------------------------------------------------------------------------
step "Detecting display"

detect_display() {
    local xorg_cmd
    xorg_cmd=$(ps aux 2>/dev/null | grep -i '[Xx]org' | head -1)

    if echo "$xorg_cmd" | grep -q '\-auth'; then
        XAUTH_PATH=$(echo "$xorg_cmd" | sed 's/.*-auth \([^ ]*\).*/\1/')
    fi

    if [[ -z "${XAUTH_PATH:-}" || ! -f "${XAUTH_PATH:-}" ]]; then
        for candidate in \
            "/run/user/$DESKTOP_UID/gdm/Xauthority" \
            "/home/$DESKTOP_USER/.Xauthority" \
            "/run/user/$DESKTOP_UID/.Xauthority"; do
            if [[ -f "$candidate" ]]; then
                XAUTH_PATH="$candidate"; break
            fi
        done
    fi

    if echo "$xorg_cmd" | grep -qE ':[0-9]+'; then
        X_DISPLAY=$(echo "$xorg_cmd" | grep -oE ':[0-9]+' | head -1)
    fi

    if [[ -n "${XAUTH_PATH:-}" ]] && DISPLAY="$X_DISPLAY" XAUTHORITY="$XAUTH_PATH" xdpyinfo &>/dev/null; then
        ok "Display: ${BOLD}$X_DISPLAY${NC}  Xauthority: ${BOLD}$XAUTH_PATH${NC}"
    elif su - "$DESKTOP_USER" -c "DISPLAY=$X_DISPLAY xdpyinfo" &>/dev/null 2>&1; then
        ok "Display: ${BOLD}$X_DISPLAY${NC}  (no explicit Xauthority needed)"
        XAUTH_PATH=""
    else
        warn "Could not verify display. Service may need manual configuration."
        warn "Edit: sudo nano /etc/systemd/system/signage-player.service"
    fi
}

detect_display

# ---------------------------------------------------------------------------
# Install bundled system packages
# ---------------------------------------------------------------------------
step "Installing system packages (offline)"

install_system_packages() {
    local distro="unknown"
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        case "${ID:-}" in
            ubuntu|debian|linuxmint|pop|elementary|zorin|raspbian) distro="debian" ;;
        esac
    fi
    command -v apt-get &>/dev/null && distro="debian"

    if [[ "$distro" != "debian" ]]; then
        warn "Non-Debian system detected. Bundled .deb packages may not install correctly."
        warn "Ensure python3, python3-venv, mpv are installed manually."
        return
    fi

    info "Installing ${DEB_COUNT} bundled packages with dpkg..."

    # Install all at once — dpkg resolves ordering automatically
    dpkg -i "$SOURCE_DIR/debs/"*.deb 2>&1 | grep -v "^(Reading\|Selecting\|Preparing\|Unpacking\|Setting\|Processing)" || true

    # Fix any broken dependency links (uses only already-installed packages — no internet)
    apt-get install -f -y --no-install-recommends 2>/dev/null || true

    # Verify critical binaries
    local missing=()
    command -v python3 &>/dev/null || missing+=("python3")
    command -v mpv     &>/dev/null || missing+=("mpv")

    if [[ ${#missing[@]} -gt 0 ]]; then
        fail "Critical packages missing after install: ${missing[*]}"
    fi

    # Check for Chromium (needed for templates, optional otherwise)
    if command -v chromium &>/dev/null || command -v chromium-browser &>/dev/null; then
        ok "Chromium installed — template playback enabled"
    else
        warn "Chromium not found — templates will be skipped on this player"
        warn "Install later with: sudo apt install chromium"
    fi

    ok "System packages installed"
}

install_system_packages

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
        --exclude='debs/' \
        --exclude='wheels/' \
        --exclude='*.pyc' \
        --exclude='offline-install.sh' \
        "$SOURCE_DIR/" "$INSTALL_DIR/"
else
    find "$INSTALL_DIR" -name '*.py' -not -path '*/venv/*' -delete 2>/dev/null || true
    cp -a "$SOURCE_DIR/." "$INSTALL_DIR/"
    rm -rf "$INSTALL_DIR/venv" "$INSTALL_DIR/__pycache__" \
           "$INSTALL_DIR/debs" "$INSTALL_DIR/wheels" \
           "$INSTALL_DIR/offline-install.sh" 2>/dev/null || true
fi

ok "Player files installed to $INSTALL_DIR"

# ---------------------------------------------------------------------------
# Python virtual environment (using bundled wheels — no internet)
# ---------------------------------------------------------------------------
step "Creating Python virtual environment"

VENV_DIR="$INSTALL_DIR/venv"
python3 -m venv "$VENV_DIR" --clear
"$VENV_DIR/bin/pip" install --upgrade pip --quiet --no-index \
    --find-links "$SOURCE_DIR/wheels" 2>/dev/null || \
    "$VENV_DIR/bin/pip" install --upgrade pip --quiet

info "Installing Python packages from bundled wheels..."
"$VENV_DIR/bin/pip" install \
    --no-index \
    --find-links "$SOURCE_DIR/wheels" \
    python-mpv httpx PyYAML aiofiles \
    --quiet

# Verify python-mpv works with system libmpv
if ! "$VENV_DIR/bin/python3" -c "import mpv" &>/dev/null 2>&1; then
    warn "python-mpv is incompatible with system libmpv — trying fallback version"
    # Try from internet as last resort
    "$VENV_DIR/bin/pip" install python-mpv==0.5.2 --quiet 2>/dev/null || true
fi

ok "Virtual environment ready"

# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------
step "Setting file ownership"

chown -R "$DESKTOP_USER:$DESKTOP_USER" "$INSTALL_DIR"
STATE_DIR="/home/$DESKTOP_USER/.config/signage-player"
mkdir -p "$STATE_DIR"
chown -R "$DESKTOP_USER:$DESKTOP_USER" "$STATE_DIR"
ok "State directory: $STATE_DIR"

# ---------------------------------------------------------------------------
# CMS URL configuration
# ---------------------------------------------------------------------------
step "Configuring CMS connection"

CONFIG_FILE="$INSTALL_DIR/config.yaml"

write_config() {
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

if [[ -f "$CONFIG_FILE" && -z "$ARG_CMS_URL" ]]; then
    ok "Existing config.yaml preserved"
elif [[ -n "$ARG_CMS_URL" ]]; then
    write_config "$ARG_CMS_URL"
    ok "CMS URL set to: $ARG_CMS_URL"
else
    # Interactive prompt
    divider
    echo ""
    echo -e "  ${BOLD}Enter your CMS server address:${NC}"
    echo ""
    echo -e "  Example: ${CYAN}http://192.168.1.100:8080${NC}"
    echo ""
    read -rp "  CMS URL: " CMS_URL
    CMS_URL="${CMS_URL%\"}"
    CMS_URL="${CMS_URL#\"}"

    if [[ -n "${CMS_URL:-}" ]]; then
        write_config "$CMS_URL"
        ok "CMS URL set to: $CMS_URL"
    else
        cp "$INSTALL_DIR/config.yaml.example" "$CONFIG_FILE" 2>/dev/null || true
        chown "$DESKTOP_USER:$DESKTOP_USER" "$CONFIG_FILE" 2>/dev/null || true
        warn "No URL entered — edit $CONFIG_FILE before starting"
    fi
fi

# ---------------------------------------------------------------------------
# Systemd service
# ---------------------------------------------------------------------------
step "Installing systemd service"

SYSTEMD_FILE="/etc/systemd/system/signage-player.service"

XAUTH_ENV_LINE=""
[[ -n "$XAUTH_PATH" ]] && XAUTH_ENV_LINE="Environment=\"XAUTHORITY=$XAUTH_PATH\""

cat > "$SYSTEMD_FILE" <<SERVICEEOF
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

Environment="DISPLAY=$X_DISPLAY"
Environment="XDG_RUNTIME_DIR=/run/user/$DESKTOP_UID"
$XAUTH_ENV_LINE

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
ok "Service installed: $SYSTEMD_FILE"

# ---------------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------------
if command -v ufw &>/dev/null; then
    if ufw status 2>/dev/null | grep -q "Status: active"; then
        ufw allow 8081/tcp &>/dev/null && ok "ufw: opened port 8081"
    fi
fi

# ---------------------------------------------------------------------------
# Enable and start
# ---------------------------------------------------------------------------
step "Starting signage-player service"

systemctl enable signage-player
systemctl restart signage-player
ok "signage-player enabled and started"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
divider
step "Installation complete"
divider
echo ""
systemctl status signage-player --no-pager --lines=5 2>/dev/null || true
echo ""
divider
echo ""
echo -e "${GREEN}${BOLD}CM:EX Linux Player installed successfully.${NC}"
echo ""
echo -e "  Desktop user:  ${BOLD}$DESKTOP_USER${NC}"
echo -e "  Display:       ${BOLD}$X_DISPLAY${NC}"
echo -e "  Install dir:   ${BOLD}$INSTALL_DIR${NC}"
echo -e "  Config:        ${BOLD}$CONFIG_FILE${NC}"
echo -e "  Logs:          ${CYAN}sudo journalctl -u signage-player -f${NC}"
echo ""
echo -e "  To change CMS URL: ${CYAN}sudo nano $CONFIG_FILE && sudo systemctl restart signage-player${NC}"
echo ""

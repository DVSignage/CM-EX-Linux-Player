#!/usr/bin/env bash
# ============================================================================
# CM:EX Linux Player — Installer
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/DVSignage/CM-EX-Linux-Player/main/install.sh | sudo bash
#   — or —
#   sudo bash install.sh
#
# Safe to run multiple times (idempotent). Re-running updates player files
# and pip packages without losing your config.yaml or enrollment state.
# ============================================================================

set -euo pipefail

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

# Determine where the source files live. When piped via curl the script runs
# from stdin, so there is no reliable BASH_SOURCE. In that case we clone from
# GitHub into a temporary directory.
SOURCE_DIR=""
TEMP_DIR=""

resolve_source() {
    # If the script was executed from a file on disk, use its directory.
    if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "bash" && -f "${BASH_SOURCE[0]}" ]]; then
        SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        if [[ -f "$SOURCE_DIR/main.py" && -f "$SOURCE_DIR/requirements.txt" ]]; then
            info "Source files found at ${BOLD}$SOURCE_DIR${NC}"
            return
        fi
    fi

    # Fallback: clone from GitHub
    info "Downloading player files from GitHub..."
    TEMP_DIR="$(mktemp -d)"
    if command -v git &>/dev/null; then
        git clone --depth 1 https://github.com/DVSignage/CM-EX-Linux-Player.git "$TEMP_DIR" \
            || fail "git clone failed. Check your network connection."
    elif command -v curl &>/dev/null; then
        curl -sSL https://github.com/DVSignage/CM-EX-Linux-Player/archive/refs/heads/main.tar.gz \
            | tar xz --strip-components=1 -C "$TEMP_DIR" \
            || fail "Download failed. Check your network connection."
    elif command -v wget &>/dev/null; then
        wget -qO- https://github.com/DVSignage/CM-EX-Linux-Player/archive/refs/heads/main.tar.gz \
            | tar xz --strip-components=1 -C "$TEMP_DIR" \
            || fail "Download failed. Check your network connection."
    else
        fail "Neither git, curl, nor wget found. Install one and try again."
    fi
    SOURCE_DIR="$TEMP_DIR"
    ok "Downloaded to $TEMP_DIR"
}

resolve_source

# Verify required source files
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
        # shellcheck disable=SC1091
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

    # Fallback: check for package managers
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
    apt-get install -y -qq python3 python3-pip python3-venv mpv libmpv-dev
}

install_deps_fedora() {
    if command -v dnf &>/dev/null; then
        dnf install -y python3 python3-pip python3-virtualenv mpv mpv-libs-devel
    else
        yum install -y python3 python3-pip python3-virtualenv mpv mpv-libs-devel
    fi
}

install_deps_arch() {
    pacman -Syu --noconfirm --needed python python-pip mpv
}

install_deps_suse() {
    zypper install -y python3 python3-pip python3-virtualenv mpv libmpv-devel
}

case "$DISTRO" in
    debian) install_deps_debian ;;
    fedora) install_deps_fedora ;;
    arch)   install_deps_arch ;;
    suse)   install_deps_suse ;;
esac

ok "System dependencies installed"

# ---------------------------------------------------------------------------
# Create install directory
# ---------------------------------------------------------------------------
INSTALL_DIR="/opt/signage-player"

step "Setting up ${BOLD}$INSTALL_DIR${NC}"

mkdir -p "$INSTALL_DIR"

# Copy player files, preserving directory structure.
# Exclude items that should not be deployed.
rsync_available=false
if command -v rsync &>/dev/null; then
    rsync_available=true
fi

if $rsync_available; then
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
    # Fallback: plain cp. Remove old .py files first to avoid stale code.
    find "$INSTALL_DIR" -name '*.py' -not -path '*/venv/*' -delete 2>/dev/null || true
    cp -a "$SOURCE_DIR/." "$INSTALL_DIR/"
    # Clean up items we don't need in the install
    rm -rf "$INSTALL_DIR/venv" 2>/dev/null || true
    rm -rf "$INSTALL_DIR/__pycache__" 2>/dev/null || true
    rm -rf "$INSTALL_DIR/.git" 2>/dev/null || true
    rm -f  "$INSTALL_DIR/.gitignore" 2>/dev/null || true
    rm -rf "$INSTALL_DIR/tests" 2>/dev/null || true
    rm -f  "$INSTALL_DIR/install.sh" 2>/dev/null || true
fi

ok "Player files copied to $INSTALL_DIR"

# ---------------------------------------------------------------------------
# Python virtual environment
# ---------------------------------------------------------------------------
step "Creating Python virtual environment"

VENV_DIR="$INSTALL_DIR/venv"

python3 -m venv "$VENV_DIR" --clear
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet

ok "Virtual environment ready at $VENV_DIR"

# ---------------------------------------------------------------------------
# System user
# ---------------------------------------------------------------------------
step "Ensuring 'signage' system user exists"

if id "signage" &>/dev/null; then
    ok "User 'signage' already exists"
else
    useradd --system --create-home --shell /usr/sbin/nologin signage
    ok "Created system user 'signage'"
fi

# Make sure the signage user owns the install directory
chown -R signage:signage "$INSTALL_DIR"

# Create the state directory so the player can write enrollment data
STATE_DIR="/home/signage/.config/signage-player"
mkdir -p "$STATE_DIR"
chown -R signage:signage "$STATE_DIR"
ok "State directory ready at $STATE_DIR"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
step "Checking configuration"

CONFIG_FILE="$INSTALL_DIR/config.yaml"

if [[ -f "$CONFIG_FILE" ]]; then
    ok "config.yaml already exists — preserving current configuration"
else
    cp "$INSTALL_DIR/config.yaml.example" "$CONFIG_FILE"
    chown signage:signage "$CONFIG_FILE"
    warn "Created config.yaml from template"
    echo ""
    echo -e "  ${BOLD}You must edit the CMS URL before starting the player:${NC}"
    echo ""
    echo -e "    ${CYAN}sudo nano $CONFIG_FILE${NC}"
    echo ""
    echo -e "  Set ${BOLD}cms_url${NC} to your CMS server address, e.g.:"
    echo -e "    cms_url: \"http://192.168.1.100:8000\""
    echo ""

    # Interactive prompt (only if stdin is a terminal)
    if [[ -t 0 ]]; then
        read -rp "  Enter your CMS URL now (or press Enter to skip): " CMS_URL
        if [[ -n "$CMS_URL" ]]; then
            # Remove surrounding quotes if the user added them
            CMS_URL="${CMS_URL%\"}"
            CMS_URL="${CMS_URL#\"}"
            sed -i "s|cms_url:.*|cms_url: \"$CMS_URL\"|" "$CONFIG_FILE"
            ok "cms_url set to $CMS_URL"
        else
            warn "Skipped — remember to edit $CONFIG_FILE before starting"
        fi
    else
        warn "Non-interactive mode — edit $CONFIG_FILE before starting the service"
    fi
fi

# ---------------------------------------------------------------------------
# Systemd service
# ---------------------------------------------------------------------------
step "Installing systemd service"

SERVICE_FILE="$INSTALL_DIR/signage-player.service"
SYSTEMD_FILE="/etc/systemd/system/signage-player.service"

# Rewrite ExecStart to use the venv python
sed "s|^ExecStart=.*|ExecStart=$VENV_DIR/bin/python3 $INSTALL_DIR/main.py|" \
    "$SERVICE_FILE" > "$SYSTEMD_FILE"

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
echo -e "  Config file:   ${BOLD}$CONFIG_FILE${NC}"
echo -e "  Install dir:   ${BOLD}$INSTALL_DIR${NC}"
echo -e "  Venv:          ${BOLD}$VENV_DIR${NC}"
echo -e "  Service:       ${BOLD}signage-player.service${NC}"
echo -e "  Logs:          ${CYAN}sudo journalctl -u signage-player -f${NC}"
echo ""
echo -e "  To reconfigure:  ${CYAN}sudo nano $CONFIG_FILE && sudo systemctl restart signage-player${NC}"
echo ""

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf "$TEMP_DIR"
fi

exit 0

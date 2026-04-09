#!/usr/bin/env bash
# ============================================================================
# CM:EX Linux Player — Offline Package Builder
#
# Run this ONCE on any machine with Docker + internet access.
# The output .run file can then be copied to players with NO internet needed.
#
# Usage:
#   bash build-offline-package.sh              # auto-version (YYYYMMDD)
#   bash build-offline-package.sh 2.1.0        # custom version
#
# Requirements:
#   - Docker (running)
#   - Internet access (for this build step only)
#
# Output:
#   cmx-player-offline-YYYYMMDD.run
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="${1:-$(date +%Y%m%d)}"
OUTPUT="${SCRIPT_DIR}/cmx-player-offline-${VERSION}.run"
UBUNTU_VERSION="22.04"

# Colours
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info() { echo -e "${CYAN}▶${NC} $*"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
fail() { echo -e "${RED}✗${NC} $*"; exit 1; }

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
command -v docker &>/dev/null || fail "Docker is required. Install Docker Desktop and try again."
docker info &>/dev/null       || fail "Docker is not running. Start Docker and try again."

echo ""
echo -e "${CYAN}${BOLD}CM:EX Player — Building offline package v${VERSION}${NC}"
echo -e "${CYAN}Target: Ubuntu ${UBUNTU_VERSION} (amd64)${NC}"
echo ""

# ---------------------------------------------------------------------------
# Temp workspace
# ---------------------------------------------------------------------------
TMPDIR=$(mktemp -d)
cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT

PKG_DIR="$TMPDIR/package"
DEBS_DIR="$PKG_DIR/debs"
WHEELS_DIR="$PKG_DIR/wheels"
mkdir -p "$DEBS_DIR" "$WHEELS_DIR"

# ---------------------------------------------------------------------------
# Step 1: Copy player source files
# ---------------------------------------------------------------------------
info "Copying player source files..."

rsync -a \
    --exclude='venv/' \
    --exclude='__pycache__/' \
    --exclude='.git/' \
    --exclude='.gitignore' \
    --exclude='config.yaml' \
    --exclude='*.pyc' \
    --exclude='tests/' \
    --exclude='*.run' \
    --exclude='build-offline-package.sh' \
    "$SCRIPT_DIR/" "$PKG_DIR/"

# Ensure offline-install.sh is included
[[ -f "$SCRIPT_DIR/offline-install.sh" ]] || fail "offline-install.sh not found in $SCRIPT_DIR"
cp "$SCRIPT_DIR/offline-install.sh" "$PKG_DIR/offline-install.sh"

ok "Source files copied"

# ---------------------------------------------------------------------------
# Step 2: Download system packages (Debian/Ubuntu .deb files)
# ---------------------------------------------------------------------------
info "Downloading system packages via Docker (Ubuntu ${UBUNTU_VERSION})..."
info "This may take a few minutes on first run..."

docker run --rm \
    -v "${DEBS_DIR}:/debs" \
    "ubuntu:${UBUNTU_VERSION}" \
    bash -c "
        set -e
        export DEBIAN_FRONTEND=noninteractive

        apt-get update -qq

        # software-properties-common lets us add PPAs
        apt-get install -y -qq software-properties-common

        # xtradeb/apps provides a real .deb chromium on Ubuntu 22.04
        # (Ubuntu ships chromium as a snap-only package from 22.04 onwards)
        add-apt-repository -y ppa:xtradeb/apps 2>/dev/null || true
        apt-get update -qq

        # Download packages + all their dependencies into the apt cache
        # --download-only fetches but does not install
        apt-get install -y -qq \
            --download-only \
            --no-install-recommends \
            python3 python3-pip python3-venv \
            mpv libmpv2 \
            chromium \
            avahi-daemon libavahi-client3 \
            curl libgtk2.0-0 2>&1 | tail -5

        # Copy everything from apt cache
        cp /var/cache/apt/archives/*.deb /debs/ 2>/dev/null || true
        echo \"Downloaded \$(ls /debs/*.deb | wc -l) packages\"
    "

DEB_COUNT=$(ls "$DEBS_DIR"/*.deb 2>/dev/null | wc -l)
[[ $DEB_COUNT -gt 0 ]] || fail "No .deb packages were downloaded. Check Docker output above."
ok "Downloaded ${DEB_COUNT} system packages"

# ---------------------------------------------------------------------------
# Step 3: Download Python wheels
# ---------------------------------------------------------------------------
info "Downloading Python wheels..."

# Read version pins from requirements.txt (skip NDI — optional and huge)
CORE_PKGS="python-mpv>=1.0.1 httpx>=0.24.1 PyYAML>=6.0 aiofiles>=23.1.0"

docker run --rm \
    -v "${WHEELS_DIR}:/wheels" \
    "ubuntu:${UBUNTU_VERSION}" \
    bash -c "
        set -e
        apt-get update -qq
        apt-get install -y -qq python3-pip
        pip3 download ${CORE_PKGS} -d /wheels --quiet
        echo \"Downloaded \$(ls /wheels/*.whl | wc -l) wheels\"
    "

WHEEL_COUNT=$(ls "$WHEELS_DIR"/*.whl 2>/dev/null | wc -l)
[[ $WHEEL_COUNT -gt 0 ]] || fail "No Python wheels were downloaded."
ok "Downloaded ${WHEEL_COUNT} Python wheels"

# ---------------------------------------------------------------------------
# Step 4: Build self-extracting .run archive
# ---------------------------------------------------------------------------
info "Packing self-extracting installer..."

HEADER="$TMPDIR/header.sh"
cat > "$HEADER" << 'HEADER_EOF'
#!/usr/bin/env bash
# CM:EX Linux Player — Offline Installer
# Usage: sudo bash cmx-player-offline-*.run [--cms URL] [--user NAME]
if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash $0 $*"
    exit 1
fi
EXTRACT_DIR=$(mktemp -d)
cleanup() { rm -rf "$EXTRACT_DIR"; }
trap cleanup EXIT
echo "Extracting package..."
SKIP=$(awk '/^__PAYLOAD__$/{print NR+1; exit}' "$0")
tail -n +$SKIP "$0" | base64 -d | tar xz -C "$EXTRACT_DIR"
echo "Running installer..."
exec bash "$EXTRACT_DIR/offline-install.sh" "$@"
exit $?
__PAYLOAD__
HEADER_EOF

(
    cat "$HEADER"
    tar czf - -C "$PKG_DIR" . | base64
) > "$OUTPUT"

chmod +x "$OUTPUT"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
SIZE=$(du -sh "$OUTPUT" | cut -f1)
echo ""
echo -e "${GREEN}${BOLD}Package ready:${NC} $(basename "$OUTPUT")  (${SIZE})"
echo ""
echo "Copy to players and install:"
echo ""
echo "  # With CMS URL (non-interactive):"
echo "  scp $(basename "$OUTPUT") user@player-ip:/tmp/"
echo "  ssh user@player-ip 'sudo bash /tmp/$(basename "$OUTPUT") --cms http://YOUR_CMS_IP:8080'"
echo ""
echo "  # Interactive (prompts for CMS URL):"
echo "  ssh user@player-ip 'sudo bash /tmp/$(basename "$OUTPUT")'"
echo ""
echo "  # USB stick / local transfer:"
echo "  sudo bash $(basename "$OUTPUT") --cms http://YOUR_CMS_IP:8080"
echo ""

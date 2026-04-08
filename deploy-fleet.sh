#!/usr/bin/env bash
# ============================================================================
# CM:EX Linux Player — Fleet Deployment Script
#
# Deploys the player to multiple machines via SSH in one go.
#
# Usage:
#   bash deploy-fleet.sh --cms http://10.0.0.5:8000 --user dvsi \
#       --hosts "10.0.0.10 10.0.0.11 10.0.0.12"
#
# Or with a hosts file (one IP per line):
#   bash deploy-fleet.sh --cms http://10.0.0.5:8000 --user dvsi \
#       --hosts-file machines.txt
#
# Prerequisites:
#   - SSH key-based auth to all target machines (ssh-copy-id first)
#   - Target user has sudo (passwordless preferred for automation)
# ============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
CMS_URL=""
DESKTOP_USER=""
HOSTS=""
HOSTS_FILE=""
SSH_USER=""
REPO_URL="https://github.com/DVSignage/CM-EX-Linux-Player.git"
PARALLEL=4  # max concurrent deployments

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cms|--cms-url)   CMS_URL="$2"; shift 2 ;;
        --user)            DESKTOP_USER="$2"; shift 2 ;;
        --hosts)           HOSTS="$2"; shift 2 ;;
        --hosts-file)      HOSTS_FILE="$2"; shift 2 ;;
        --ssh-user)        SSH_USER="$2"; shift 2 ;;
        --parallel)        PARALLEL="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: bash deploy-fleet.sh [OPTIONS]"
            echo ""
            echo "Required:"
            echo "  --cms URL           CMS server URL"
            echo "  --hosts \"IP1 IP2\"   Space-separated list of target IPs"
            echo "  OR --hosts-file F   File with one IP per line"
            echo ""
            echo "Optional:"
            echo "  --user NAME         Desktop user on targets (auto-detected if omitted)"
            echo "  --ssh-user NAME     SSH login user (default: current user)"
            echo "  --parallel N        Max concurrent deploys (default: 4)"
            echo ""
            echo "Example:"
            echo "  bash deploy-fleet.sh --cms http://cms:8000 --user dvsi \\"
            echo "      --hosts \"10.0.0.10 10.0.0.11 10.0.0.12 10.0.0.13\""
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

[[ -z "$CMS_URL" ]] && { echo -e "${RED}Error: --cms is required${NC}"; exit 1; }

# Build host list
HOST_LIST=()
if [[ -n "$HOSTS" ]]; then
    read -ra HOST_LIST <<< "$HOSTS"
fi
if [[ -n "$HOSTS_FILE" && -f "$HOSTS_FILE" ]]; then
    while IFS= read -r line; do
        line="${line%%#*}"  # strip comments
        line="${line// /}"  # strip whitespace
        [[ -n "$line" ]] && HOST_LIST+=("$line")
    done < "$HOSTS_FILE"
fi

if [[ ${#HOST_LIST[@]} -eq 0 ]]; then
    echo -e "${RED}Error: No hosts specified. Use --hosts or --hosts-file${NC}"
    exit 1
fi

echo -e "${CYAN}${BOLD}CM:EX Fleet Deployment${NC}"
echo -e "  CMS URL: ${BOLD}$CMS_URL${NC}"
echo -e "  Targets: ${BOLD}${#HOST_LIST[@]}${NC} machines"
[[ -n "$DESKTOP_USER" ]] && echo -e "  User:    ${BOLD}$DESKTOP_USER${NC}"
echo ""

# Build the remote install command
USER_FLAG=""
[[ -n "$DESKTOP_USER" ]] && USER_FLAG="--user $DESKTOP_USER"
SSH_PREFIX=""
[[ -n "$SSH_USER" ]] && SSH_PREFIX="${SSH_USER}@"

REMOTE_CMD="rm -rf /tmp/cmx-player && git clone --depth 1 $REPO_URL /tmp/cmx-player && sudo bash /tmp/cmx-player/install.sh --cms \"$CMS_URL\" $USER_FLAG && rm -rf /tmp/cmx-player"

# ---------------------------------------------------------------------------
# Deploy to each host
# ---------------------------------------------------------------------------
LOG_DIR="/tmp/cmx-fleet-deploy-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$LOG_DIR"

deploy_one() {
    local host="$1"
    local logfile="$LOG_DIR/${host}.log"

    echo -e "  ${CYAN}[DEPLOYING]${NC} $host ..."

    if ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
        "${SSH_PREFIX}${host}" "$REMOTE_CMD" > "$logfile" 2>&1; then
        echo -e "  ${GREEN}[OK]${NC}        $host"
        return 0
    else
        echo -e "  ${RED}[FAILED]${NC}    $host (see $logfile)"
        return 1
    fi
}

# Run deployments with parallelism limit
PIDS=()
HOSTS_RUNNING=()
SUCCESS=0
FAIL=0

for host in "${HOST_LIST[@]}"; do
    # Wait if at parallel limit
    while [[ ${#PIDS[@]} -ge $PARALLEL ]]; do
        NEW_PIDS=()
        NEW_HOSTS=()
        for i in "${!PIDS[@]}"; do
            if kill -0 "${PIDS[$i]}" 2>/dev/null; then
                NEW_PIDS+=("${PIDS[$i]}")
                NEW_HOSTS+=("${HOSTS_RUNNING[$i]}")
            else
                wait "${PIDS[$i]}" && SUCCESS=$((SUCCESS+1)) || FAIL=$((FAIL+1))
            fi
        done
        PIDS=("${NEW_PIDS[@]+"${NEW_PIDS[@]}"}")
        HOSTS_RUNNING=("${NEW_HOSTS[@]+"${NEW_HOSTS[@]}"}")
        [[ ${#PIDS[@]} -ge $PARALLEL ]] && sleep 1
    done

    deploy_one "$host" &
    PIDS+=($!)
    HOSTS_RUNNING+=("$host")
done

# Wait for remaining
for i in "${!PIDS[@]}"; do
    wait "${PIDS[$i]}" && SUCCESS=$((SUCCESS+1)) || FAIL=$((FAIL+1))
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${CYAN}${BOLD}Deployment complete${NC}"
echo -e "  ${GREEN}Success: $SUCCESS${NC}"
echo -e "  ${RED}Failed:  $FAIL${NC}"
echo -e "  Logs:    ${BOLD}$LOG_DIR/${NC}"
echo ""

if [[ $FAIL -gt 0 ]]; then
    echo -e "${YELLOW}Failed hosts — check logs and retry:${NC}"
    for host in "${HOST_LIST[@]}"; do
        if [[ -f "$LOG_DIR/${host}.log" ]] && ! grep -q '\[OK\]' "$LOG_DIR/${host}.log" 2>/dev/null; then
            echo "  $host"
        fi
    done
    exit 1
fi

exit 0

# =============================================================================
# CM:EX Linux Player - Fleet Update Script (Windows PowerShell)
#
# Pushes the player code (main.py, cmx/, ndi/, requirements) to every player,
# installs any new Python packages and restarts the service.
#
# Prerequisites:
#   - OpenSSH client and tar (both built into Windows 10/11)
#   - SSH key authentication set up OR password auth
#   - Players reachable on the network from this machine
#   - Players have internet access for pip (new packages only)
#
# Usage:
#   .\deploy-update.ps1
#   .\deploy-update.ps1 -User dvsi -KeyFile "C:\Users\you\.ssh\id_rsa"
#   .\deploy-update.ps1 -Source D:\Codes\dvsi\CM-EX-Linux-Player
#
# =============================================================================

param(
    [string]$User    = "dvsi",
    [string]$KeyFile = "",   # leave blank to use default SSH key
    [string]$Source  = "."   # folder containing main.py and cmx/
)

# -- Player IP addresses - edit this list -------------------------------------
$Players = @(
    "192.168.14.101",
    "192.168.14.102",
    "192.168.14.103",
    "192.168.14.104",
    "192.168.14.105",
    "192.168.14.106",
    "192.168.14.107",
    "192.168.14.108",
    "192.168.14.109",
    "192.168.14.110",
    "192.168.14.111",
    "192.168.14.112",
    "192.168.14.113",
    "192.168.14.114",
    "192.168.14.115",
    "192.168.14.116"
)

# -----------------------------------------------------------------------------

$ErrorActionPreference = "Continue"

$SshOpts = @("-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10")
if ($KeyFile -ne "") {
    $SshOpts += @("-i", $KeyFile)
}

$SourceFull = Resolve-Path $Source
foreach ($required in @("main.py", "cmx", "requirements.txt")) {
    if (-not (Test-Path (Join-Path $SourceFull $required))) {
        Write-Host "ERROR: $required not found in $SourceFull." -ForegroundColor Red
        exit 1
    }
}

# Bundle the player code into one archive so each player needs one scp.
$Bundle = Join-Path $env:TEMP "cmx-player-update.tar.gz"
$Items = @("main.py", "cmx", "requirements.txt")
foreach ($optional in @("ndi", "requirements-ndi.txt")) {
    if (Test-Path (Join-Path $SourceFull $optional)) { $Items += $optional }
}
& tar -czf $Bundle --exclude="__pycache__" --exclude="*.pyc" -C $SourceFull @Items
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: could not create $Bundle" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "CM:EX Linux Player - Fleet Update" -ForegroundColor Cyan
Write-Host "==================================" -ForegroundColor Cyan
Write-Host "  Source : $SourceFull ($($Items -join ', '))"
Write-Host "  User   : $User"
Write-Host "  Players: $($Players.Count)"
Write-Host ""

# Runs on the player: unpack, copy into place as the install owner, install
# new pip packages (best effort for NDI), restart.
$RemoteCmd = @'
set -e
D=/opt/signage-player
OWNER=$(stat -c %U "$D")
rm -rf /tmp/cmx-update && mkdir -p /tmp/cmx-update
tar -xzf /tmp/cmx-player-update.tar.gz -C /tmp/cmx-update
sudo rm -rf "$D/cmx"
sudo cp -r /tmp/cmx-update/. "$D/"
sudo chown -R "$OWNER:$OWNER" "$D"
sudo -u "$OWNER" "$D/venv/bin/pip" install -q -r "$D/requirements.txt"
if [ -f "$D/requirements-ndi.txt" ]; then sudo -u "$OWNER" "$D/venv/bin/pip" install -q -r "$D/requirements-ndi.txt" || true; fi
sudo systemctl restart signage-player
echo OK
'@ -replace "`r", ""

$Results = @()

foreach ($IP in $Players) {
    Write-Host "[$IP] Connecting..." -NoNewline

    # 1. Copy the bundle to the player
    $ScpArgs = $SshOpts + @($Bundle, "${User}@${IP}:/tmp/cmx-player-update.tar.gz")
    $scpResult = & scp @ScpArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host " FAILED (scp)" -ForegroundColor Red
        $Results += [PSCustomObject]@{ IP = $IP; Status = "FAILED (scp)"; Detail = $scpResult }
        continue
    }

    # 2. Install it and restart the service
    $SshArgs = $SshOpts + @("${User}@${IP}", "bash -s")
    $sshResult = $RemoteCmd | & ssh @SshArgs 2>&1
    if ($LASTEXITCODE -ne 0 -or ($sshResult -join "`n") -notmatch "OK") {
        Write-Host " FAILED (ssh)" -ForegroundColor Red
        $Results += [PSCustomObject]@{ IP = $IP; Status = "FAILED (ssh)"; Detail = $sshResult }
        continue
    }

    Write-Host " OK" -ForegroundColor Green
    $Results += [PSCustomObject]@{ IP = $IP; Status = "OK"; Detail = "" }
}

Remove-Item $Bundle -ErrorAction SilentlyContinue

# -- Summary ------------------------------------------------------------------
Write-Host ""
Write-Host "Summary" -ForegroundColor Cyan
Write-Host "-------"
$ok      = ($Results | Where-Object { $_.Status -eq "OK" }).Count
$failed  = ($Results | Where-Object { $_.Status -ne "OK" }).Count
Write-Host "  Success : $ok / $($Players.Count)" -ForegroundColor Green
if ($failed -gt 0) {
    Write-Host "  Failed  : $failed" -ForegroundColor Red
    Write-Host ""
    Write-Host "Failed players:" -ForegroundColor Yellow
    $Results | Where-Object { $_.Status -ne "OK" } | ForEach-Object {
        Write-Host "  $($_.IP) - $($_.Status)"
        if ($_.Detail) { Write-Host "    $($_.Detail)" -ForegroundColor DarkGray }
    }
}
Write-Host ""

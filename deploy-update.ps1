# =============================================================================
# CM:EX Linux Player — Fleet Update Script (Windows PowerShell)
#
# Pushes the latest main.py to all players and restarts the service.
#
# Prerequisites:
#   - OpenSSH installed on this Windows machine (built-in on Windows 10/11)
#     Start > Settings > Apps > Optional Features > OpenSSH Client
#   - SSH key authentication set up OR password auth (see below)
#   - Players reachable on the network from this machine
#
# Usage:
#   .\deploy-update.ps1
#   .\deploy-update.ps1 -User dvsi -KeyFile "C:\Users\you\.ssh\id_rsa"
#
# =============================================================================

param(
    [string]$User    = "dvsi",
    [string]$KeyFile = "",          # leave blank to use default SSH key
    [string]$MainPy  = ".\main.py"  # path to the updated main.py on this machine
)

# ── Player IP addresses — edit this list ─────────────────────────────────────
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

# ─────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Continue"

# Build SSH/SCP option strings
$SshOpts = @("-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10")
if ($KeyFile -ne "") {
    $SshOpts += @("-i", $KeyFile)
}

# Verify main.py exists
if (-not (Test-Path $MainPy)) {
    Write-Host "ERROR: $MainPy not found. Run this script from the folder containing main.py." -ForegroundColor Red
    exit 1
}

$MainPyFull = Resolve-Path $MainPy

Write-Host ""
Write-Host "CM:EX Linux Player — Fleet Update" -ForegroundColor Cyan
Write-Host "==================================" -ForegroundColor Cyan
Write-Host "  File   : $MainPyFull"
Write-Host "  User   : $User"
Write-Host "  Players: $($Players.Count)"
Write-Host ""

$Results = @()

foreach ($IP in $Players) {
    Write-Host "[$IP] Connecting..." -NoNewline

    # 1. Copy main.py to the player
    $ScpArgs = $SshOpts + @($MainPyFull, "${User}@${IP}:/tmp/main.py")
    $scpResult = & scp @ScpArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host " FAILED (scp)" -ForegroundColor Red
        $Results += [PSCustomObject]@{ IP = $IP; Status = "FAILED (scp)"; Detail = $scpResult }
        continue
    }

    # 2. Move it into place and restart the service
    $RemoteCmd = "sudo cp /tmp/main.py /opt/signage-player/main.py && sudo systemctl restart signage-player && echo OK"
    $SshArgs = $SshOpts + @("${User}@${IP}", $RemoteCmd)
    $sshResult = & ssh @$SshArgs 2>&1
    if ($LASTEXITCODE -ne 0 -or $sshResult -notmatch "OK") {
        Write-Host " FAILED (ssh)" -ForegroundColor Red
        $Results += [PSCustomObject]@{ IP = $IP; Status = "FAILED (ssh)"; Detail = $sshResult }
        continue
    }

    Write-Host " OK" -ForegroundColor Green
    $Results += [PSCustomObject]@{ IP = $IP; Status = "OK"; Detail = "" }
}

# ── Summary ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Summary" -ForegroundColor Cyan
Write-Host "───────"
$ok      = ($Results | Where-Object { $_.Status -eq "OK" }).Count
$failed  = ($Results | Where-Object { $_.Status -ne "OK" }).Count
Write-Host "  Success : $ok / $($Players.Count)" -ForegroundColor Green
if ($failed -gt 0) {
    Write-Host "  Failed  : $failed" -ForegroundColor Red
    Write-Host ""
    Write-Host "Failed players:" -ForegroundColor Yellow
    $Results | Where-Object { $_.Status -ne "OK" } | ForEach-Object {
        Write-Host "  $($_.IP) — $($_.Status)"
        if ($_.Detail) { Write-Host "    $($_.Detail)" -ForegroundColor DarkGray }
    }
}
Write-Host ""

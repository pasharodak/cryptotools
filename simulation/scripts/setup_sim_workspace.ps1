# CryptoTools: site (live) + simulation workspace
param(
    [string]$LiveRoot = "D:\cryptotools\site",
    [string]$SimRoot = "D:\cryptotools\simulation"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $LiveRoot)) {
    throw "Live project not found: $LiveRoot"
}

Write-Host "Creating sim workspace at $SimRoot"
New-Item -ItemType Directory -Force -Path $SimRoot | Out-Null

function Link-OrCopy {
    param([string]$Source, [string]$Target)
    if (Test-Path $Target) { return }
    $parent = Split-Path $Target -Parent
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    try {
        New-Item -ItemType Junction -Path $Target -Target $Source | Out-Null
        Write-Host "  junction $Target -> $Source"
    } catch {
        Copy-Item -Recurse -Force $Source $Target
        Write-Host "  copied $Target"
    }
}

# Share venv and strategies (read-only via junction)
Link-OrCopy (Join-Path $LiveRoot ".venv") (Join-Path $SimRoot ".venv")
Link-OrCopy (Join-Path $LiveRoot "simulation") (Join-Path $SimRoot "simulation")
Link-OrCopy (Join-Path $LiveRoot "user_data\strategies") (Join-Path $SimRoot "user_data\strategies")

# Configs live inside simulation/ (reachable via junction — no separate copy needed)

# Marker file — bots must not start live from sim root without this check
@"
SIM_MODE=1
LIVE_ROOT=$LiveRoot
SIM_ROOT=$SimRoot
# API ports for optional dry-run against sim exchange: 9080/9081/9082
"@ | Set-Content -Encoding UTF8 (Join-Path $SimRoot ".env.sim")

Write-Host @"

Sim workspace ready: $SimRoot

Live project ($LiveRoot) is NOT modified.
Run replay from live root:
  .\simulation\scripts\run_sim_replay.ps1

Or start exchange simulator API:
  .\.venv\Scripts\python.exe simulation\exchange_sim\server.py
"@

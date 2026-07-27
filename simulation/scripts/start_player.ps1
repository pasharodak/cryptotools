# Start sim player UI + API
param(
    [switch]$SkipDownload
)

$ErrorActionPreference = "Stop"
$Crypto = "D:\cryptotools"
$Site   = Join-Path $Crypto "site"
$Sim    = Join-Path $Crypto "simulation"
Set-Location $Crypto

$py = Join-Path $Site ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Python venv not found: $py" }
$env:PYTHONPATH = "D:\cryptotools;D:\cryptotools\site\scripts"
& $py -m pip install -q -r (Join-Path $Sim "requirements.txt") 2>&1 | Out-Null

if (-not $SkipDownload) {
    & (Join-Path $Sim "scripts\download_player_data.ps1")
}

Write-Host "=== Starting Sim Player on http://127.0.0.1:18999 ==="
Start-Process "http://127.0.0.1:18999"
& $py (Join-Path $Sim "exchange_sim\server.py")

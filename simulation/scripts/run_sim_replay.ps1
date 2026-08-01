# Full counterfactual replay pipeline
param(
    [switch]$SkipFetch,
    [switch]$SkipDownload,
    [switch]$StartSimApi
)

$ErrorActionPreference = "Stop"
$Crypto = if (Test-Path "D:\cryptotools\simulation") {
    "D:\cryptotools"
} else {
    Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
}
$Site = Join-Path $Crypto "site"
$Sim = Join-Path $Crypto "simulation"
Set-Location $Crypto

$py = Join-Path $Site ".venv\Scripts\python.exe"
$ft = Join-Path $Site ".venv\Scripts\ctbot.exe"

if (-not (Test-Path $ft)) {
    throw "Site venv not found at $ft. Create site\.venv first."
}

$env:PYTHONPATH = "$Crypto;$Site\scripts"

# Optional: sim deps
& $py -m pip install -q -r (Join-Path $Sim "requirements.txt") 2>$null

if ($StartSimApi) {
    Start-Process -FilePath $py -ArgumentList (Join-Path $Sim "exchange_sim\server.py") -WorkingDirectory $Crypto
    Write-Host "Exchange sim API starting on http://127.0.0.1:18999"
}

if (-not $SkipFetch) {
    Write-Host "=== 1/4 Fetch live trade DBs from VPS ==="
    & (Join-Path $Sim "scripts\fetch_vps_trades.ps1")
}

Write-Host "=== 2/4 Export trade scenarios ==="
& $py (Join-Path $Sim "integration\export_trades.py")

if (-not $SkipDownload) {
    Write-Host "=== 3/4 Download historical candles ==="
    & $py (Join-Path $Sim "integration\download_history.py")
}

Write-Host "=== 4/4 Run counterfactual backtests ==="
& $py (Join-Path $Sim "integration\run_counterfactual.py")

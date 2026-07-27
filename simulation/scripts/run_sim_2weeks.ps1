# 2-week simulation: all traded pairs, baseline vs improved
param(
    [switch]$SkipFetch,
    [switch]$SkipDownload
)

$ErrorActionPreference = "Stop"
$Crypto = "D:\cryptotools"
$Site = Join-Path $Crypto "site"
$Sim = Join-Path $Crypto "simulation"
Set-Location $Crypto
$py = Join-Path $Site ".venv\Scripts\python.exe"
$env:PYTHONPATH = "$Crypto;$Site\scripts"

if (-not $SkipFetch) {
    Write-Host "=== Fetch live DBs from VPS ==="
    & powershell -ExecutionPolicy Bypass -File (Join-Path $Sim "scripts\fetch_vps_trades.ps1")
}

Write-Host "=== Export trade pairs ==="
& $py (Join-Path $Sim "integration\export_trades.py")

if (-not $SkipDownload) {
    Write-Host "=== Download 2 weeks OHLCV ==="
    & $py (Join-Path $Sim "integration\download_history.py")
}

Write-Host "=== Run counterfactual ==="
& $py (Join-Path $Sim "integration\run_counterfactual.py")

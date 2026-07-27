# Live trading with ~18 USDT on Bybit futures (FreqAI)
# WARNING: real money. Stop with Ctrl+C or Stop-Process.

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
. "$Root\scripts\load_env.ps1"

if ($env:BYBIT_TESTNET -eq "true") {
    Write-Warning "BYBIT_TESTNET=true — keys must match testnet, not mainnet balance."
}

Write-Host "LIVE mode: stake 5 USDT, max 1 open trade, dry_run=false"
Write-Host "Press Ctrl+C to stop."

& "$Root\.venv\Scripts\freqtrade.exe" trade `
    --config user_data\config.json `
    --strategy CriptoFreqaiHybridStrategy `
    --freqaimodel LightGBMRegressor `
    --logfile user_data\logs\freqtrade-live.log

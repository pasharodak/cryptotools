$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
. "$Root\scripts\load_env.ps1"

$args = @(
    "trade",
    "--config", "user_data\config.json",
    "--strategy", "CriptoFreqaiHybridStrategy",
    "--freqaimodel", "LightGBMRegressor",
    "--logfile", "user_data\logs\freqtrade.log"
)

Write-Host "Starting Freqtrade + FreqAI (LightGBMRegressor, dry-run)..."
& "$Root\.venv\Scripts\freqtrade.exe" @args

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

Set-Location $Root
. "$Root\scripts\load_env.ps1"

$logOut = Join-Path $Root "user_data\logs\freqtrade.out.log"
$logErr = Join-Path $Root "user_data\logs\freqtrade.err.log"
New-Item -ItemType Directory -Force -Path (Split-Path $logOut) | Out-Null

$args = @(
    "trade",
    "--config", "user_data\config.json",
    "--strategy", "CriptoFreqaiHybridStrategy",
    "--freqaimodel", "LightGBMRegressor",
    "--logfile", "user_data\logs\freqtrade.log"
)

Write-Host "Starting Freqtrade (dry_run=$(if ((Get-Content user_data\config.json -Raw) -match '\"dry_run\":\s*true') {'yes'} else {'NO'}))..."
& "$Root\.venv\Scripts\freqtrade.exe" @args 2>&1 | Tee-Object -FilePath $logOut

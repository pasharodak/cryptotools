<#
.SYNOPSIS
  Поднять локально панель CryptoTools как на проде (UI + pair-config + nginx-proxy).

.DESCRIPTION
  - pair-config на :8090
  - gateway (static custom-ui + API proxy) на :8443 (HTTP)
  - Боты strategy/grid/finder по умолчанию НЕ стартуют (live-торговля).
    Передай -WithBots чтобы поднять dry-run ботов из локальных config_*.json.

.EXAMPLE
  cd D:\cryptotools\site\scripts
  .\start_local_site.ps1
#>
param(
    [switch]$WithBots,
    [switch]$SkipPairConfig,
    [int]$Port = 8443,
    [string]$HostAddr = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$SiteRoot = Split-Path $PSScriptRoot -Parent
$Py = Join-Path $SiteRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    throw "venv not found: $Py"
}

. (Join-Path $PSScriptRoot "load_env.ps1")

$env:CT_BASE = $SiteRoot
$env:CT_ENV = Join-Path $SiteRoot ".env"
$env:PAIR_CONFIG_HOST = "127.0.0.1"
$env:PAIR_CONFIG_PORT = "8090"
$env:LOCAL_SITE_HOST = $HostAddr
$env:LOCAL_SITE_PORT = "$Port"
# Ensure scripts/ is importable for pair_config_server deps
$env:PYTHONPATH = $PSScriptRoot

Write-Host "CT_BASE=$env:CT_BASE"
Write-Host "UI+API gateway -> http://${HostAddr}:${Port}/"
Write-Host "Login: FREQUI_USERNAME / FREQUI_PASSWORD from site\.env"

$procs = @()

function Start-Bg([string]$Name, [string]$FilePath, [string[]]$ArgumentList, [string]$WorkDir) {
    $out = Join-Path $env:TEMP "ct_local_${Name}.out.log"
    $err = Join-Path $env:TEMP "ct_local_${Name}.err.log"
    $p = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -WorkingDirectory $WorkDir `
        -PassThru -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
    Write-Host "started $Name pid=$($p.Id)  logs: $out"
    return $p
}

try {
    if (-not $SkipPairConfig) {
        $procs += Start-Bg "pair-config" $Py @((Join-Path $PSScriptRoot "pair_config_server.py")) $SiteRoot
        Start-Sleep -Seconds 2
    }

    $procs += Start-Bg "gateway" $Py @(
        (Join-Path $PSScriptRoot "local_site_gateway.py"),
        "--host", $HostAddr,
        "--port", "$Port"
    ) $SiteRoot

    if ($WithBots) {
        Write-Host "Starting shared stack (signal-engine:8081 + trade-executor)..."
        $ctbot = Join-Path $SiteRoot ".venv\Scripts\ctbot.exe"
        if (-not (Test-Path $ctbot)) { $ctbot = Join-Path $SiteRoot ".venv\Scripts\freqtrade.exe" }
        $signalCfg = Join-Path $SiteRoot "user_data\config_signal_engine.json"
        if ((Test-Path $ctbot) -and (Test-Path $signalCfg)) {
            $env:CT_SIGNAL_ONLY = "1"
            $procs += Start-Bg "signal-engine" $ctbot @(
                "trade", "--config", "user_data\config_signal_engine.json",
                "--strategy", "MultiStrategyRouter",
                "--strategy-path", "user_data\strategies",
                "--logfile", "user_data\logs\cryptotools-strategy.log"
            ) $SiteRoot
        } else {
            Write-Warning "signal-engine skipped (ctbot or config_signal_engine.json missing)"
        }
        $procs += Start-Bg "trade-executor" $Py @(
            (Join-Path $PSScriptRoot "trade_executor.py")
        ) $SiteRoot
        $gridCfg = Join-Path $SiteRoot "user_data\config_grid.json"
        if ((Test-Path $ctbot) -and (Test-Path $gridCfg)) {
            $procs += Start-Bg "grid" $ctbot @(
                "trade", "--config", "user_data\config_grid.json",
                "--strategy", "VolatilityGridStrategy",
                "--strategy-path", "user_data\strategies",
                "--logfile", "user_data\logs\cryptotools-grid.log"
            ) $SiteRoot
        }
        $finderCfg = Join-Path $SiteRoot "user_data\config.json"
        if ((Test-Path $ctbot) -and (Test-Path $finderCfg)) {
            $procs += Start-Bg "finder" $ctbot @(
                "trade", "--config", "user_data\config.json",
                "--strategy", "TradeFinderStrategy",
                "--strategy-path", "user_data\strategies",
                "--logfile", "user_data\logs\cryptotools-finder.log"
            ) $SiteRoot
        }
    } else {
        Write-Host "Bots not started. UI will show offline until: .\start_local_site.ps1 -WithBots"
    }

    Start-Sleep -Seconds 1
    try {
        $h = Invoke-WebRequest -Uri "http://${HostAddr}:8090/health" -UseBasicParsing -TimeoutSec 5
        Write-Host "pair-config health: $($h.StatusCode)"
    } catch {
        Write-Warning "pair-config health check failed: $_"
    }

    Write-Host ""
    Write-Host "Open:  http://${HostAddr}:${Port}/"
    Write-Host "Press Ctrl+C to stop all."
    Write-Host ""

    while ($true) {
        Start-Sleep -Seconds 2
        foreach ($p in $procs) {
            if ($p.HasExited) {
                Write-Warning "process exited pid=$($p.Id) code=$($p.ExitCode)"
            }
        }
    }
}
finally {
    foreach ($p in $procs) {
        if ($p -and -not $p.HasExited) {
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            Write-Host "stopped pid=$($p.Id)"
        }
    }
}

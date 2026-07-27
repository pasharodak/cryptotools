# Download 1s candles for sim player pairs
param(
    [string]$Timerange = "",
    [string[]]$Pairs = @()
)

$ErrorActionPreference = "Stop"
$Crypto = "D:\cryptotools"
$Site = Join-Path $Crypto "site"
$Sim = Join-Path $Crypto "simulation"
Set-Location $Crypto

$py = Join-Path $Site ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Python venv not found: $py" }
& $py -m pip install -q pyarrow pandas 2>$null

$args = @((Join-Path $Sim "integration\download_subminute.py"))
if ($Timerange) { $args += @("--timerange", $Timerange) }
if ($Pairs.Count) { $args += @("--pairs") + $Pairs }

Write-Host "=== Download sub-minute (1s) data ==="
& $py @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Done. Start player: .\simulation\scripts\start_player.ps1"

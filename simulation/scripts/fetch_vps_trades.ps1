# Fetch live trade DBs from VPS (read-only) for counterfactual replay
param(
    [string]$VpsHost = "root@77.222.35.209",
    [string]$SshKey = "D:\cryptotools\site\deploy\id_rsa\id_rsa",
    [string]$RemoteBase = "/home/freqtrade/freqtrade"
)

$ErrorActionPreference = "Stop"
$Crypto = if (Test-Path "D:\cryptotools\simulation") {
    "D:\cryptotools"
} else {
    Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
}

$Dest = Join-Path $Crypto "simulation\data\live_dbs"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

$ssh = "C:\Program Files\Git\usr\bin\ssh.exe"
$scp = $ssh -replace "ssh.exe", "scp.exe"

$files = @(
    @{ Remote = "tradesv3-finder.sqlite"; Local = "tradesv3-finder.sqlite" },
    @{ Remote = "tradesv3-strategy.sqlite"; Local = "tradesv3-strategy.sqlite" },
    @{ Remote = "tradesv3-grid.sqlite"; Local = "tradesv3-grid.sqlite" }
)

foreach ($f in $files) {
    Write-Host "Fetching $($f.Remote) ..."
    & $scp -i $SshKey -o StrictHostKeyChecking=no "${VpsHost}:${RemoteBase}/$($f.Remote)" (Join-Path $Dest $f.Local)
}
Write-Host "Done -> $Dest"

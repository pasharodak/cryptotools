# Deploy live_grid ML model to VPS (model files only + optional gate config)
param(
    [string]$ServerIp = "77.222.35.209",
    [string]$SshUser = "root",
    [string]$KeyPath = "D:\cryptotools\site\deploy\id_rsa\id_rsa",
    [switch]$SkipGate
)

$ErrorActionPreference = "Stop"
$ssh = if (Test-Path "C:\Program Files\Git\usr\bin\ssh.exe") { "C:\Program Files\Git\usr\bin\ssh.exe" } else { "ssh" }
$scp = $ssh -replace "ssh.exe", "scp.exe"
$root = "D:\cryptotools\site"
$gridLocal = "$root\user_data\models\pnl_classifier\by_scenario\live_grid"
$meta = "$gridLocal\pnl_classifier_meta.json"

if (-not (Test-Path "$gridLocal\pnl_classifier.joblib")) {
    Write-Error "Grid model not found: $gridLocal\pnl_classifier.joblib - run train_grid_model.py first"
}

Write-Host "=== Sync model registry (live_grid) ==="
Push-Location $root
& .\.venv\Scripts\python.exe simulation\scripts\sync_model_registry.py --only live_grid --note "prod grid deploy"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "=== Upload grid model to VPS ==="
$remoteGrid = "/home/cryptotools/app/user_data/models/pnl_classifier/by_scenario/live_grid"
$remoteHost = $SshUser + '@' + $ServerIp
& $scp -i $KeyPath -r "$gridLocal\pnl_classifier.joblib" "$gridLocal\pnl_classifier_meta.json" ($remoteHost + ':' + $remoteGrid + '/')

if (-not $SkipGate) {
    Write-Host "=== Upload ml_entry_gate.json ==="
    & $scp -i $KeyPath "$root\user_data\ml_entry_gate.json" ($remoteHost + ':/home/cryptotools/app/user_data/')
}

$deployNote = Get-Content $meta -Raw | ConvertFrom-Json
Write-Host ("Model: " + $deployNote.model_key + " n=" + $deployNote.n_trades + " AUC " + $deployNote.roc_auc)

Write-Host "=== Restart cryptotools-grid ==="
$remoteCmd = 'chown -R cryptotools:cryptotools /home/cryptotools/app/user_data/models/pnl_classifier/by_scenario/live_grid; systemctl restart cryptotools-grid; sleep 5; systemctl is-active cryptotools-grid; journalctl -u cryptotools-grid -n 6 --no-pager'
& $ssh -i $KeyPath ($SshUser + '@' + $ServerIp) $remoteCmd
Pop-Location
Write-Host "Done. Grid model deployed."


param(
    [string]$ServerIp = "77.222.35.209",
    [string]$SshUser = "root",
    [string]$KeyPath = "D:\cryptotools\site\deploy\id_rsa\id_rsa"
)

$ErrorActionPreference = "Stop"
$ssh = "C:\Program Files\Git\usr\bin\ssh.exe"
$scp = $ssh -replace "ssh.exe", "scp.exe"

Write-Host "Deploy dual bots + custom UI to ${SshUser}@${ServerIp}..."
& $ssh -i $KeyPath -o ConnectTimeout=20 "${SshUser}@${ServerIp}" "echo SSH_OK"

$files = @(
    "D:\cryptotools\site\user_data\config.json",
    "D:\cryptotools\site\user_data\config_strategy.json",
    "D:\cryptotools\site\user_data\strategies\MultiStrategyRouter.py",
    "D:\cryptotools\site\user_data\strategies\CriptoPairsStrategy.py",
    "D:\cryptotools\site\user_data\strategies\SupertrendStrategy.py",
    "D:\cryptotools\site\user_data\strategies\MacdEmaStrategy.py",
    "D:\cryptotools\site\user_data\strategies\FibPullbackStrategy.py",
    "D:\cryptotools\site\user_data\enabled_strategies.json",
    "D:\cryptotools\site\user_data\strategies\TripleEmaStrategy.py",
    "D:\cryptotools\site\user_data\strategies\BollingerRsiStrategy.py",
    "D:\cryptotools\site\user_data\strategies\AdxMomentumStrategy.py",
    "D:\cryptotools\site\user_data\strategies\VolatilityGridStrategy.py",
    "D:\cryptotools\site\deploy\cryptotools-finder.service",
    "D:\cryptotools\site\deploy\cryptotools-strategy.service",
    "D:\cryptotools\site\deploy\cryptotools-grid.service",
    "D:\cryptotools\site\deploy\nginx-cryptotools.conf",
    "D:\cryptotools\site\deploy\setup-dual-ui.sh",
    "D:\cryptotools\site\deploy\pair-config.service",
    "D:\cryptotools\site\user_data\ranging_scan_config.json",
    "D:\cryptotools\site\deploy\ranging-scanner.service",
    "D:\cryptotools\site\deploy\ranging-scanner.timer",
    "D:\cryptotools\site\scripts\scan_ranging_pairs.py",
    "D:\cryptotools\site\user_data\strategy_scan_config.json",
    "D:\cryptotools\site\deploy\strategy-scanner.service",
    "D:\cryptotools\site\deploy\strategy-scanner.timer",
    "D:\cryptotools\site\scripts\scan_strategy_pairs.py",
    "D:\cryptotools\site\scripts\run_strategy_scan.sh",
    "D:\cryptotools\site\scripts\pair_config_server.py",
    "D:\cryptotools\site\scripts\bybit_grid_manager.py",
    "D:\cryptotools\site\user_data\bybit_grid_config.json"
)
foreach ($f in $files) {
    $name = Split-Path $f -Leaf
    & $scp -i $KeyPath $f "${SshUser}@${ServerIp}:/tmp/$name"
}

& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "mkdir -p /tmp/custom-ui"
& $scp -i $KeyPath "D:\cryptotools\site\custom-ui\index.html" "D:\cryptotools\site\custom-ui\app.js" "D:\cryptotools\site\custom-ui\styles.css" "${SshUser}@${ServerIp}:/tmp/custom-ui/"

& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "sed -i 's/\r$//' /tmp/setup-dual-ui.sh; chmod +x /tmp/setup-dual-ui.sh; bash /tmp/setup-dual-ui.sh"

Write-Host ""
Write-Host "Custom UI: https://${ServerIp}:8443"
Write-Host "ML Finder max 3 | Strategy max 2 | Grid max 2 (total 7)"
Write-Host "Login: FREQUI_USERNAME / FREQUI_PASSWORD in D:\cryptotools\site\.env"


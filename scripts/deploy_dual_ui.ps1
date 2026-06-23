param(
    [string]$ServerIp = "77.222.35.209",
    [string]$SshUser = "root",
    [string]$KeyPath = "D:\criptotools\id_rsa\id_rsa"
)

$ErrorActionPreference = "Stop"
$ssh = "C:\Program Files\Git\usr\bin\ssh.exe"
$scp = $ssh -replace "ssh.exe", "scp.exe"

Write-Host "Deploy dual bots + custom UI to ${SshUser}@${ServerIp}..."
& $ssh -i $KeyPath -o ConnectTimeout=20 "${SshUser}@${ServerIp}" "echo SSH_OK"

$files = @(
    "D:\freqtrade\user_data\config.json",
    "D:\freqtrade\user_data\config_strategy.json",
    "D:\freqtrade\user_data\strategies\MultiStrategyRouter.py",
    "D:\freqtrade\user_data\strategies\CriptoPairsStrategy.py",
    "D:\freqtrade\user_data\strategies\SupertrendStrategy.py",
    "D:\freqtrade\user_data\strategies\MacdEmaStrategy.py",
    "D:\freqtrade\user_data\strategies\TripleEmaStrategy.py",
    "D:\freqtrade\user_data\strategies\BollingerRsiStrategy.py",
    "D:\freqtrade\user_data\strategies\AdxMomentumStrategy.py",
    "D:\freqtrade\user_data\strategies\VolatilityGridStrategy.py",
    "D:\freqtrade\deploy\freqtrade.service",
    "D:\freqtrade\deploy\freqtrade-strategy.service",
    "D:\freqtrade\deploy\freqtrade-grid.service",
    "D:\freqtrade\deploy\nginx-freqtrade.conf",
    "D:\freqtrade\deploy\setup-dual-ui.sh",
    "D:\freqtrade\deploy\pair-config.service",
    "D:\freqtrade\user_data\ranging_scan_config.json",
    "D:\freqtrade\deploy\ranging-scanner.service",
    "D:\freqtrade\deploy\ranging-scanner.timer",
    "D:\freqtrade\scripts\scan_ranging_pairs.py",
    "D:\freqtrade\user_data\strategy_scan_config.json",
    "D:\freqtrade\deploy\strategy-scanner.service",
    "D:\freqtrade\deploy\strategy-scanner.timer",
    "D:\freqtrade\scripts\scan_strategy_pairs.py",
    "D:\freqtrade\scripts\run_strategy_scan.sh",
    "D:\freqtrade\scripts\pair_config_server.py"
)
foreach ($f in $files) {
    $name = Split-Path $f -Leaf
    & $scp -i $KeyPath $f "${SshUser}@${ServerIp}:/tmp/$name"
}

& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "mkdir -p /tmp/custom-ui"
& $scp -i $KeyPath "D:\freqtrade\custom-ui\index.html" "D:\freqtrade\custom-ui\app.js" "D:\freqtrade\custom-ui\styles.css" "${SshUser}@${ServerIp}:/tmp/custom-ui/"

& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "sed -i 's/\r$//' /tmp/setup-dual-ui.sh; chmod +x /tmp/setup-dual-ui.sh; bash /tmp/setup-dual-ui.sh"

Write-Host ""
Write-Host "Custom UI: https://${ServerIp}:8443"
Write-Host "FreqAI max 3 | Strategy max 2 | Grid max 2 (total 7)"
Write-Host "Login: FREQUI_USERNAME / FREQUI_PASSWORD in D:\criptotools\.env"

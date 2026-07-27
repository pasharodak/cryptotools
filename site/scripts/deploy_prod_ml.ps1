# Deploy prod ML pack to VPS: ML Finder + strategy bot + grid
param(
    [string]$ServerIp = "77.222.35.209",
    [string]$SshUser = "root",
    [string]$KeyPath = "D:\cryptotools\site\deploy\id_rsa\id_rsa"
)

$ErrorActionPreference = "Stop"
$ssh = if (Test-Path "C:\Program Files\Git\usr\bin\ssh.exe") { "C:\Program Files\Git\usr\bin\ssh.exe" } else { "ssh" }
$scp = $ssh -replace "ssh.exe", "scp.exe"

Write-Host "=== Apply prod ML config locally ==="
$Site = "D:\cryptotools\site"
$Sim  = "D:\cryptotools\simulation"
Push-Location $Site
& .\.venv\Scripts\python.exe "$Sim\scripts\apply_prod_ml_config.py"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "=== Upload user_data (ML gate, finder, strategies, config, models) ==="
& $scp -i $KeyPath user_data/enabled_strategies.json user_data/bot_strategies.json user_data/config.json user_data/config_strategy.json user_data/config_grid.json user_data/ml_training_pairs_whitelist.json user_data/pairlist_mode.json user_data/ml_entry_gate.json user_data/strategy_scan_config.json user_data/ranging_scan_config.json user_data/trade_finder.json user_data/finder_bot.json "${SshUser}@${ServerIp}:/home/freqtrade/freqtrade/user_data/"
& $scp -i $KeyPath -r user_data/ml "${SshUser}@${ServerIp}:/home/freqtrade/freqtrade/user_data/"
& $scp -i $KeyPath -r user_data/models/pnl_classifier/pnl_classifier.joblib user_data/models/pnl_classifier/pnl_classifier_meta.json "${SshUser}@${ServerIp}:/home/freqtrade/freqtrade/user_data/models/pnl_classifier/"
& $scp -i $KeyPath -r user_data/models/pnl_classifier/by_scenario/live_grid "${SshUser}@${ServerIp}:/home/freqtrade/freqtrade/user_data/models/pnl_classifier/by_scenario/"
foreach ($sid in @("trend_supertrend", "trend_macd_ema", "trend_fib", "lite_mean_rev", "trend_breakout", "lite_range")) {
    $local = "user_data/models/pnl_classifier/by_scenario/$sid"
    if (Test-Path $local) {
        & $ssh -i $KeyPath "${SshUser}@${ServerIp}" "mkdir -p /home/freqtrade/freqtrade/user_data/models/pnl_classifier/by_scenario/$sid"
        & $scp -i $KeyPath -r "$local/*" "${SshUser}@${ServerIp}:/home/freqtrade/freqtrade/user_data/models/pnl_classifier/by_scenario/$sid/"
    }
}
& $scp -i $KeyPath -r user_data/models/trade_finder "${SshUser}@${ServerIp}:/home/freqtrade/freqtrade/user_data/models/"
& $scp -i $KeyPath user_data/strategies/TradeFinderStrategy.py user_data/strategies/MultiStrategyRouter.py user_data/strategies/VolatilityGridStrategy.py user_data/strategies/_sim_live.py user_data/strategies/TripleEmaStrategy.py user_data/strategies/AdxMomentumStrategy.py user_data/strategies/BollingerRsiStrategy.py user_data/strategies/LiteIntradayStrategy.py user_data/strategies/LiteRangeStrategy.py user_data/strategies/SupertrendStrategy.py user_data/strategies/MacdEmaStrategy.py user_data/strategies/FibPullbackStrategy.py "${SshUser}@${ServerIp}:/home/freqtrade/freqtrade/user_data/strategies/"
& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "mkdir -p /home/freqtrade/freqtrade/simulation/config /home/freqtrade/freqtrade/simulation/strategies"
& $scp -i $KeyPath -r "$Sim\strategies" "$Sim\__init__.py" "${SshUser}@${ServerIp}:/home/freqtrade/freqtrade/simulation/"
& $scp -i $KeyPath "$Sim\config\ml_training_pairs_whitelist.json" "${SshUser}@${ServerIp}:/home/freqtrade/freqtrade/simulation/config/"
& $scp -i $KeyPath deploy/freqtrade.service deploy/disable-finder-bot.sh "${SshUser}@${ServerIp}:/home/freqtrade/freqtrade/deploy/"
& $scp -i $KeyPath scripts/pair_config_server.py scripts/scan_strategy_pairs.py scripts/grid_changelog.py scripts/apply_pairlist_mode.py "${SshUser}@${ServerIp}:/home/freqtrade/freqtrade/scripts/"
& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "mkdir -p /tmp/custom-ui"
& $scp -i $KeyPath custom-ui/index.html custom-ui/app.js custom-ui/styles.css "${SshUser}@${ServerIp}:/tmp/custom-ui/"

Write-Host "=== VPS: restart strategy + grid (ML Finder stays disabled) ==="
& $ssh -i $KeyPath "${SshUser}@${ServerIp}" @"
set -e
cp /tmp/custom-ui/index.html /tmp/custom-ui/app.js /tmp/custom-ui/styles.css /var/www/criptotools-ui/
chown -R www-data:www-data /var/www/criptotools-ui
chown -R freqtrade:freqtrade /home/freqtrade/freqtrade/user_data /home/freqtrade/freqtrade/simulation/strategies /home/freqtrade/freqtrade/scripts/pair_config_server.py /home/freqtrade/freqtrade/scripts/scan_strategy_pairs.py /home/freqtrade/freqtrade/scripts/grid_changelog.py
cd /home/freqtrade/freqtrade && sudo -u freqtrade ./.venv/bin/python -c 'import sys; sys.path.insert(0,\"scripts\"); from grid_changelog import ensure_seeded; ensure_seeded(); print(\"changelog ok\")'
systemctl daemon-reload
systemctl restart pair-config
systemctl restart freqtrade-strategy freqtrade-grid
systemctl enable freqtrade-strategy freqtrade-grid
bash /home/freqtrade/freqtrade/deploy/disable-finder-bot.sh freqtrade.service
sleep 5
systemctl is-active freqtrade || true
systemctl is-active pair-config
systemctl is-active freqtrade-strategy
systemctl is-active freqtrade-grid
"@
Pop-Location
Write-Host "Done. Strategy + grid running; ML Finder disabled."


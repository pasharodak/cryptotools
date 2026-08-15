# Deploy prod ML pack to VPS: ML Finder + strategy bot + grid
param(
    [string]$ServerIp = "77.222.35.209",
    [string]$SshUser = "root",
    [string]$KeyPath = "D:\cryptotools\site\deploy\id_rsa\id_rsa"
)

$ErrorActionPreference = "Stop"
$ssh = if (Test-Path "C:\Program Files\Git\usr\bin\ssh.exe") { "C:\Program Files\Git\usr\bin\ssh.exe" } else { "ssh" }
$scp = $ssh -replace "ssh.exe", "scp.exe"

$Site = "D:\cryptotools\site"
$Sim  = "D:\cryptotools\simulation"
Push-Location $Site

Write-Host "=== Pull live UI strategy toggles from VPS (source of truth) ==="
$LocalEn = "user_data/enabled_strategies.json"
$LocalBot = "user_data/bot_strategies.json"
$RemoteUd = "/home/cryptotools/app/user_data"
& $scp -i $KeyPath "${SshUser}@${ServerIp}:${RemoteUd}/enabled_strategies.json" "$LocalEn"
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARN: could not pull enabled_strategies.json from VPS"
} else {
    & $scp -i $KeyPath "${SshUser}@${ServerIp}:${RemoteUd}/bot_strategies.json" "$LocalBot"
    Write-Host "pulled enabled_strategies.json + bot_strategies.json from VPS"
}

Write-Host "=== Apply prod ML config locally (preserves UI enables) ==="
& .\.venv\Scripts\python.exe "$Sim\scripts\apply_prod_ml_config.py"
if ($LASTEXITCODE -ne 0) { Pop-Location; exit $LASTEXITCODE }

Write-Host "=== Upload user_data (ML gate, finder, strategies, config, models) ==="
# NOTE: do NOT upload enabled_strategies.json / bot_strategies.json — VPS UI toggles are sacred.
& $scp -i $KeyPath user_data/config.json user_data/config_strategy.json user_data/config_grid.json user_data/ml_training_pairs_whitelist.json user_data/pairlist_mode.json user_data/ml_entry_gate.json user_data/strategy_scan_config.json user_data/ranging_scan_config.json user_data/trade_finder.json user_data/finder_bot.json "${SshUser}@${ServerIp}:/home/cryptotools/app/user_data/"
& $scp -i $KeyPath -r user_data/ml "${SshUser}@${ServerIp}:/home/cryptotools/app/user_data/"
& $scp -i $KeyPath -r user_data/models/pnl_classifier/pnl_classifier.joblib user_data/models/pnl_classifier/pnl_classifier_meta.json "${SshUser}@${ServerIp}:/home/cryptotools/app/user_data/models/pnl_classifier/"
& $scp -i $KeyPath -r user_data/models/pnl_classifier/by_scenario/live_grid "${SshUser}@${ServerIp}:/home/cryptotools/app/user_data/models/pnl_classifier/by_scenario/"
foreach ($sid in @("scalp_liq_breakout", "new_psar", "chart3_atrch", "chart2_cmf", "scalp_ema", "chart3_adosc", "new_donchian", "chart3_ppo", "combo_don_adx_vol", "chart2_obv", "chart3_elder", "scalp_macd", "new_keltner", "chart_ha", "chart2_vortex", "chart3_ao", "combo_kc_stoch_vol", "chart3_tema", "chart2_trix", "chart3_roc", "combo_hma_ppo_atr", "chart_willr", "chart_squeeze", "combo_ema_rsi_atr", "chart2_aroon", "combo_adx_macd_vol", "chart2_engulf", "chart_adxdi", "chart2_mfi", "new_ichimoku", "combo_st_rsi_obv", "trend_breakout", "lite_mean_rev", "trend_macd_ema", "trend_supertrend", "trend_ema", "lite_range", "lite_intraday", "trend_fib", "new_psar_test", "chart3_atrch_test", "trend_breakout_test", "trend_supertrend_test", "chart2_cmf_test", "scalp_ema_test", "chart3_adosc_test", "new_donchian_test", "chart3_ppo_test", "combo_don_adx_vol_test", "chart2_obv_test", "chart3_elder_test", "scalp_liq_breakout_test", "lite_mean_rev_test", "trend_macd_ema_test")) {
    $local = "user_data/models/pnl_classifier/by_scenario/$sid"
    if (Test-Path $local) {
        & $ssh -i $KeyPath "${SshUser}@${ServerIp}" "mkdir -p /home/cryptotools/app/user_data/models/pnl_classifier/by_scenario/$sid"
        & $scp -i $KeyPath -r "$local/*" "${SshUser}@${ServerIp}:/home/cryptotools/app/user_data/models/pnl_classifier/by_scenario/$sid/"
    }
}
& $scp -i $KeyPath -r user_data/models/trade_finder "${SshUser}@${ServerIp}:/home/cryptotools/app/user_data/models/"
& $scp -i $KeyPath -r user_data/strategies "${SshUser}@${ServerIp}:/home/cryptotools/app/user_data/"
# Keep only .py strategy sources on VPS (drop local __pycache__ if scp brought it)
& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "rm -rf /home/cryptotools/app/user_data/strategies/__pycache__"
& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "mkdir -p /home/cryptotools/app/simulation/config /home/cryptotools/app/simulation/strategies /home/cryptotools/app/simulation/ml"
& $scp -i $KeyPath -r "$Sim\strategies" "$Sim\ml" "$Sim\__init__.py" "${SshUser}@${ServerIp}:/home/cryptotools/app/simulation/"
& $scp -i $KeyPath "$Sim\config\ml_training_pairs_whitelist.json" "$Sim\config\prod_top30_pack.json" "$Sim\config\prod_ml_bots.json" "${SshUser}@${ServerIp}:/home/cryptotools/app/simulation/config/"
if (Test-Path "user_data/grid_changelog.json") {
    Write-Host "skip uploading local grid_changelog.json (VPS changelog is source of truth)"
}
& $scp -i $KeyPath deploy/cryptotools-finder.service deploy/disable-finder-bot.sh "${SshUser}@${ServerIp}:/home/cryptotools/app/deploy/"
& $scp -i $KeyPath scripts/pair_config_server.py scripts/tenant_manager.py scripts/tenant_context.py scripts/bybit_grid_manager.py scripts/scan_strategy_pairs.py scripts/grid_changelog.py scripts/apply_pairlist_mode.py scripts/merge_enabled_strategies_from_pack.py "${SshUser}@${ServerIp}:/home/cryptotools/app/scripts/"
& $scp -i $KeyPath deploy/cryptotools-strategy@.service deploy/cryptotools-grid@.service deploy/cryptotools-finder@.service deploy/sudoers-cryptotools-tenants deploy/tenant-systemctl.sh "${SshUser}@${ServerIp}:/home/cryptotools/app/deploy/"
& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "mkdir -p /tmp/custom-ui"
& $scp -i $KeyPath custom-ui/index.html custom-ui/app.js custom-ui/styles.css "${SshUser}@${ServerIp}:/tmp/custom-ui/"

Write-Host "=== VPS: restart strategy + grid (ML Finder stays disabled) ==="
$remoteSh = Join-Path $env:TEMP "cryptotools_deploy_restart.sh"
$remoteBody = @"
set -e
cp /tmp/custom-ui/index.html /tmp/custom-ui/app.js /tmp/custom-ui/styles.css /var/www/criptotools-ui/
chown -R www-data:www-data /var/www/criptotools-ui
chown -R cryptotools:cryptotools /home/cryptotools/app/user_data /home/cryptotools/app/simulation/strategies /home/cryptotools/app/scripts
install -m 644 /home/cryptotools/app/deploy/cryptotools-strategy@.service /etc/systemd/system/cryptotools-strategy@.service
install -m 644 /home/cryptotools/app/deploy/cryptotools-grid@.service /etc/systemd/system/cryptotools-grid@.service
install -m 644 /home/cryptotools/app/deploy/cryptotools-finder@.service /etc/systemd/system/cryptotools-finder@.service
chmod 755 /home/cryptotools/app/deploy/tenant-systemctl.sh
install -m 440 /home/cryptotools/app/deploy/sudoers-cryptotools-tenants /etc/sudoers.d/cryptotools-tenants
visudo -cf /etc/sudoers.d/cryptotools-tenants
if ! grep -q '^SECRETS_MASTER_KEY=' /home/cryptotools/.cryptotools.env 2>/dev/null; then
  python3 -c "import secrets,pathlib; p=pathlib.Path('/home/cryptotools/.cryptotools.env'); t=p.read_text(encoding='utf-8') if p.is_file() else ''; p.write_text(t + ('\n' if t and not t.endswith('\n') else '') + 'SECRETS_MASTER_KEY=' + secrets.token_urlsafe(32) + '\n', encoding='utf-8'); print('SECRETS_MASTER_KEY added')"
  chmod 600 /home/cryptotools/.cryptotools.env
fi
# Preserve UI strategy toggles: only add missing pack ids as OFF
sudo -u cryptotools /home/cryptotools/app/.venv/bin/python /home/cryptotools/app/scripts/merge_enabled_strategies_from_pack.py
cd /home/cryptotools/app && sudo -u cryptotools ./.venv/bin/python -c 'import sys; sys.path.insert(0,"scripts"); from grid_changelog import ensure_seeded; ensure_seeded(); print("changelog ok")'
systemctl daemon-reload
systemctl restart pair-config
systemctl restart cryptotools-strategy cryptotools-grid
systemctl enable cryptotools-strategy cryptotools-grid
bash /home/cryptotools/app/deploy/disable-finder-bot.sh cryptotools-finder.service
sleep 5
systemctl is-active cryptotools-finder || true
systemctl is-active pair-config
systemctl is-active cryptotools-strategy
systemctl is-active cryptotools-grid
# Smoke: health + auth login endpoint exists
curl -s -o /dev/null -w 'health:%{http_code}\n' http://127.0.0.1:8090/health || true
"@
# Force LF so bash on VPS does not see CR as part of commands
[System.IO.File]::WriteAllText($remoteSh, ($remoteBody -replace "`r`n", "`n" -replace "`r", "`n"))
& $scp -i $KeyPath $remoteSh "${SshUser}@${ServerIp}:/tmp/cryptotools_deploy_restart.sh"
& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "bash /tmp/cryptotools_deploy_restart.sh"
Pop-Location
Write-Host "Done. Strategy + grid running; ML Finder disabled. UI enables were NOT overwritten."


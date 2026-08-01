#!/bin/bash
# Deploy optimization pack G/S/F and restart bots.
set -euo pipefail
cd /home/freqtrade/freqtrade
source /home/freqtrade/.freqtrade.env

echo "=== Close stale ML Finder positions ==="
bash scripts/force_exit_all.sh 8080 || true

echo "=== Ranging scan → grid whitelist ==="
.venv/bin/python3 scripts/scan_ranging_pairs.py || true

echo "=== Strategy scan → strategy whitelist ==="
.venv/bin/python3 scripts/scan_strategy_pairs.py || true

echo "=== Reseed changelog v3 ==="
.venv/bin/python3 -c "from scripts.grid_changelog import ensure_seeded; ensure_seeded()" 2>/dev/null \
  || .venv/bin/python3 -c "import sys; sys.path.insert(0,'scripts'); from grid_changelog import ensure_seeded; ensure_seeded()"

echo "=== Restart bots (requires root) ==="
if [ "$(id -u)" -eq 0 ]; then
  systemctl restart freqtrade freqtrade-strategy freqtrade-grid pair-config
else
  echo "Run as root: systemctl restart freqtrade freqtrade-strategy freqtrade-grid pair-config"
fi

sleep 5
for svc in freqtrade freqtrade-strategy freqtrade-grid pair-config; do
  printf "%s: " "$svc"
  systemctl is-active "$svc" || true
done

echo "=== Open trades after restart ==="
bash scripts/force_exit_all.sh 8080
bash scripts/check_open_trades.sh 2>/dev/null || true

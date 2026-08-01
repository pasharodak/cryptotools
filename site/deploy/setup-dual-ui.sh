#!/bin/bash
set -e
UI_DIR=/var/www/criptotools-ui
mkdir -p "$UI_DIR"
if [ -d /tmp/custom-ui ]; then
  cp /tmp/custom-ui/index.html /tmp/custom-ui/app.js /tmp/custom-ui/styles.css "$UI_DIR/"
elif [ -d /tmp/custom-ui/custom-ui ]; then
  cp /tmp/custom-ui/custom-ui/index.html /tmp/custom-ui/custom-ui/app.js /tmp/custom-ui/custom-ui/styles.css "$UI_DIR/"
fi
# UI is deployed via /tmp/custom-ui — do not overwrite from stale /tmp/*.html|js|css
rm -rf "$UI_DIR/custom-ui"
chown -R www-data:www-data "$UI_DIR" 2>/dev/null || chown -R root:root "$UI_DIR"

# Remove bundled FreqUI — only CriptoTools custom panel is used
FREQUI_DIR=/home/freqtrade/freqtrade/freqtrade/rpc/api_server/ui/installed
if [ -d "$FREQUI_DIR" ]; then
  find "$FREQUI_DIR" -mindepth 1 -delete
fi

install -m 644 /tmp/nginx-freqtrade.conf /etc/nginx/sites-available/freqtrade
ln -sf /etc/nginx/sites-available/freqtrade /etc/nginx/sites-enabled/freqtrade
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

install -m 644 /tmp/pair_config_server.py /home/freqtrade/freqtrade/scripts/pair_config_server.py
install -m 644 /tmp/pair-config.service /etc/systemd/system/pair-config.service
chown freqtrade:freqtrade /home/freqtrade/freqtrade/scripts/pair_config_server.py

# Snapshot user settings from live configs before deploy overwrites them
sudo -u freqtrade env FT_BASE=/home/freqtrade/freqtrade FT_ENV=/home/freqtrade/.freqtrade.env \
  /home/freqtrade/freqtrade/.venv/bin/python3 \
  /home/freqtrade/freqtrade/scripts/pair_config_server.py ensure-limits || true
sudo -u freqtrade env FT_BASE=/home/freqtrade/freqtrade FT_ENV=/home/freqtrade/.freqtrade.env \
  /home/freqtrade/freqtrade/.venv/bin/python3 \
  /home/freqtrade/freqtrade/scripts/pair_config_server.py snapshot-strategies || true

install -m 644 /tmp/freqtrade.service /etc/systemd/system/freqtrade.service
install -m 644 /tmp/freqtrade-strategy.service /etc/systemd/system/freqtrade-strategy.service
install -m 644 /tmp/freqtrade-grid.service /etc/systemd/system/freqtrade-grid.service
cp /tmp/config.json /home/freqtrade/freqtrade/user_data/config.json
for strat in CriptoPairsStrategy SupertrendStrategy MacdEmaStrategy TripleEmaStrategy BollingerRsiStrategy AdxMomentumStrategy MultiStrategyRouter; do
  if [ -f "/tmp/${strat}.py" ]; then
    cp "/tmp/${strat}.py" "/home/freqtrade/freqtrade/user_data/strategies/${strat}.py"
  fi
done
# enabled_strategies.json and config_grid.json / config_strategy whitelists — scanners on server
if [ ! -f /home/freqtrade/freqtrade/user_data/config_strategy.json ] && [ -f /tmp/config_strategy.json ]; then
  cp /tmp/config_strategy.json /home/freqtrade/freqtrade/user_data/config_strategy.json
fi
if [ -f /tmp/VolatilityGridStrategy.py ]; then
  cp /tmp/VolatilityGridStrategy.py /home/freqtrade/freqtrade/user_data/strategies/VolatilityGridStrategy.py
fi
install -m 644 /tmp/scan_ranging_pairs.py /home/freqtrade/freqtrade/scripts/scan_ranging_pairs.py 2>/dev/null || true
if [ -f /tmp/run_ranging_scan.sh ]; then
  install -m 755 /tmp/run_ranging_scan.sh /home/freqtrade/freqtrade/scripts/run_ranging_scan.sh
fi
if [ -f /tmp/ranging_scan_config.json ]; then
  cp /tmp/ranging_scan_config.json /home/freqtrade/freqtrade/user_data/ranging_scan_config.json
fi
install -m 644 /tmp/ranging-scanner.service /etc/systemd/system/ranging-scanner.service 2>/dev/null || true
install -m 644 /tmp/ranging-scanner.timer /etc/systemd/system/ranging-scanner.timer 2>/dev/null || true
install -m 644 /tmp/strategy-scanner.service /etc/systemd/system/strategy-scanner.service 2>/dev/null || true
install -m 644 /tmp/strategy-scanner.timer /etc/systemd/system/strategy-scanner.timer 2>/dev/null || true
if [ -f /tmp/scan_strategy_pairs.py ]; then
  install -m 644 /tmp/scan_strategy_pairs.py /home/freqtrade/freqtrade/scripts/scan_strategy_pairs.py
fi
if [ -f /tmp/run_strategy_scan.sh ]; then
  install -m 755 /tmp/run_strategy_scan.sh /home/freqtrade/freqtrade/scripts/run_strategy_scan.sh
fi
if [ -f /tmp/strategy_scan_config.json ]; then
  cp /tmp/strategy_scan_config.json /home/freqtrade/freqtrade/user_data/strategy_scan_config.json
fi
chown freqtrade:freqtrade /home/freqtrade/freqtrade/scripts/scan_ranging_pairs.py 2>/dev/null || true
chown freqtrade:freqtrade /home/freqtrade/freqtrade/scripts/scan_strategy_pairs.py 2>/dev/null || true

# Restore user settings after config deploy
sudo -u freqtrade env FT_BASE=/home/freqtrade/freqtrade FT_ENV=/home/freqtrade/.freqtrade.env \
  /home/freqtrade/freqtrade/.venv/bin/python3 \
  /home/freqtrade/freqtrade/scripts/pair_config_server.py apply-limits || true
sudo -u freqtrade env FT_BASE=/home/freqtrade/freqtrade FT_ENV=/home/freqtrade/.freqtrade.env \
  /home/freqtrade/freqtrade/.venv/bin/python3 \
  /home/freqtrade/freqtrade/scripts/pair_config_server.py apply-strategies || true

chown -R freqtrade:freqtrade /home/freqtrade/freqtrade/user_data
systemctl daemon-reload
systemctl enable freqtrade-strategy freqtrade-grid pair-config ranging-scanner.timer strategy-scanner.timer
if [ -f "$APP/deploy/disable-finder-bot.sh" ]; then
  bash "$APP/deploy/disable-finder-bot.sh" freqtrade.service
else
  systemctl disable --now freqtrade.service 2>/dev/null || true
  systemctl mask freqtrade.service 2>/dev/null || true
fi
systemctl restart pair-config
systemctl restart freqtrade
systemctl restart freqtrade-strategy
systemctl restart freqtrade-grid
systemctl restart ranging-scanner.timer 2>/dev/null || true
systemctl restart strategy-scanner.timer 2>/dev/null || true
sleep 25
echo "=== status ==="
systemctl is-active freqtrade freqtrade-strategy freqtrade-grid pair-config nginx
curl -sk https://127.0.0.1:8443/api/finder/ping || true
echo ""
curl -sk https://127.0.0.1:8443/api/strategy/ping || true
echo ""
curl -sk https://127.0.0.1:8443/api/grid/ping || true
echo ""

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
FREQUI_DIR=/home/cryptotools/app/ctengine/rpc/api_server/ui/installed
if [ -d "$FREQUI_DIR" ]; then
  find "$FREQUI_DIR" -mindepth 1 -delete
fi

install -m 644 /tmp/nginx-cryptotools.conf /etc/nginx/sites-available/cryptotools
ln -sf /etc/nginx/sites-available/cryptotools /etc/nginx/sites-enabled/cryptotools
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

install -m 644 /tmp/pair_config_server.py /home/cryptotools/app/scripts/pair_config_server.py
install -m 644 /tmp/pair-config.service /etc/systemd/system/pair-config.service
chown cryptotools:cryptotools /home/cryptotools/app/scripts/pair_config_server.py

# Snapshot user settings from live configs before deploy overwrites them
sudo -u cryptotools env CT_BASE=/home/cryptotools/app CT_ENV=/home/cryptotools/.cryptotools.env \
  /home/cryptotools/app/.venv/bin/python3 \
  /home/cryptotools/app/scripts/pair_config_server.py ensure-limits || true
sudo -u cryptotools env CT_BASE=/home/cryptotools/app CT_ENV=/home/cryptotools/.cryptotools.env \
  /home/cryptotools/app/.venv/bin/python3 \
  /home/cryptotools/app/scripts/pair_config_server.py snapshot-strategies || true

install -m 644 /tmp/cryptotools-finder.service /etc/systemd/system/cryptotools-finder.service
install -m 644 /tmp/cryptotools-strategy.service /etc/systemd/system/cryptotools-strategy.service
install -m 644 /tmp/cryptotools-grid.service /etc/systemd/system/cryptotools-grid.service
cp /tmp/config.json /home/cryptotools/app/user_data/config.json
for strat in CriptoPairsStrategy SupertrendStrategy MacdEmaStrategy TripleEmaStrategy BollingerRsiStrategy AdxMomentumStrategy MultiStrategyRouter; do
  if [ -f "/tmp/${strat}.py" ]; then
    cp "/tmp/${strat}.py" "/home/cryptotools/app/user_data/strategies/${strat}.py"
  fi
done
# enabled_strategies.json and config_grid.json / config_strategy whitelists — scanners on server
if [ ! -f /home/cryptotools/app/user_data/config_strategy.json ] && [ -f /tmp/config_strategy.json ]; then
  cp /tmp/config_strategy.json /home/cryptotools/app/user_data/config_strategy.json
fi
if [ -f /tmp/VolatilityGridStrategy.py ]; then
  cp /tmp/VolatilityGridStrategy.py /home/cryptotools/app/user_data/strategies/VolatilityGridStrategy.py
fi
install -m 644 /tmp/scan_ranging_pairs.py /home/cryptotools/app/scripts/scan_ranging_pairs.py 2>/dev/null || true
if [ -f /tmp/run_ranging_scan.sh ]; then
  install -m 755 /tmp/run_ranging_scan.sh /home/cryptotools/app/scripts/run_ranging_scan.sh
fi
if [ -f /tmp/ranging_scan_config.json ]; then
  cp /tmp/ranging_scan_config.json /home/cryptotools/app/user_data/ranging_scan_config.json
fi
install -m 644 /tmp/ranging-scanner.service /etc/systemd/system/ranging-scanner.service 2>/dev/null || true
install -m 644 /tmp/ranging-scanner.timer /etc/systemd/system/ranging-scanner.timer 2>/dev/null || true
install -m 644 /tmp/strategy-scanner.service /etc/systemd/system/strategy-scanner.service 2>/dev/null || true
install -m 644 /tmp/strategy-scanner.timer /etc/systemd/system/strategy-scanner.timer 2>/dev/null || true
if [ -f /tmp/scan_strategy_pairs.py ]; then
  install -m 644 /tmp/scan_strategy_pairs.py /home/cryptotools/app/scripts/scan_strategy_pairs.py
fi
if [ -f /tmp/run_strategy_scan.sh ]; then
  install -m 755 /tmp/run_strategy_scan.sh /home/cryptotools/app/scripts/run_strategy_scan.sh
fi
if [ -f /tmp/strategy_scan_config.json ]; then
  cp /tmp/strategy_scan_config.json /home/cryptotools/app/user_data/strategy_scan_config.json
fi
chown cryptotools:cryptotools /home/cryptotools/app/scripts/scan_ranging_pairs.py 2>/dev/null || true
chown cryptotools:cryptotools /home/cryptotools/app/scripts/scan_strategy_pairs.py 2>/dev/null || true

# Restore user settings after config deploy
sudo -u cryptotools env CT_BASE=/home/cryptotools/app CT_ENV=/home/cryptotools/.cryptotools.env \
  /home/cryptotools/app/.venv/bin/python3 \
  /home/cryptotools/app/scripts/pair_config_server.py apply-limits || true
sudo -u cryptotools env CT_BASE=/home/cryptotools/app CT_ENV=/home/cryptotools/.cryptotools.env \
  /home/cryptotools/app/.venv/bin/python3 \
  /home/cryptotools/app/scripts/pair_config_server.py apply-strategies || true

chown -R cryptotools:cryptotools /home/cryptotools/app/user_data
systemctl daemon-reload
systemctl enable cryptotools-strategy cryptotools-grid pair-config ranging-scanner.timer strategy-scanner.timer
if [ -f "$APP/deploy/disable-finder-bot.sh" ]; then
  bash "$APP/deploy/disable-finder-bot.sh" cryptotools-finder.service
else
  systemctl disable --now cryptotools-finder.service 2>/dev/null || true
  systemctl mask cryptotools-finder.service 2>/dev/null || true
fi
systemctl restart pair-config
systemctl restart cryptotools-finder
systemctl restart cryptotools-strategy
systemctl restart cryptotools-grid
systemctl restart ranging-scanner.timer 2>/dev/null || true
systemctl restart strategy-scanner.timer 2>/dev/null || true
sleep 25
echo "=== status ==="
systemctl is-active cryptotools-finder cryptotools-strategy cryptotools-grid pair-config nginx
curl -sk https://127.0.0.1:8443/api/finder/ping || true
echo ""
curl -sk https://127.0.0.1:8443/api/strategy/ping || true
echo ""
curl -sk https://127.0.0.1:8443/api/grid/ping || true
echo ""

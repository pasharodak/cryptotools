#!/bin/bash
set -euo pipefail
APP=/home/cryptotools/app
cp /tmp/signal_bus.py /tmp/user_exchange.py /tmp/user_trading.py /tmp/trade_executor.py "$APP/scripts/"
cp /tmp/pair_config_server.py "$APP/scripts/"
cp /tmp/MultiStrategyRouter.py "$APP/user_data/strategies/"
cp /tmp/config_signal_engine.json "$APP/user_data/"
cp /tmp/trading_enabled.json "$APP/user_data/"
mkdir -p "$APP/user_data/tenants/simeon"
printf '%s\n' '{"strategy": true, "grid": false, "finder": false}' > "$APP/user_data/tenants/simeon/trading_enabled.json"
cp /tmp/cryptotools-signal-engine.service /tmp/trade-executor.service /tmp/enable-shared-bots.sh "$APP/deploy/"
cp /tmp/app.js /var/www/criptotools-ui/app.js
chown -R cryptotools:cryptotools \
  "$APP/scripts/signal_bus.py" \
  "$APP/scripts/user_exchange.py" \
  "$APP/scripts/user_trading.py" \
  "$APP/scripts/trade_executor.py" \
  "$APP/scripts/pair_config_server.py" \
  "$APP/user_data/strategies/MultiStrategyRouter.py" \
  "$APP/user_data/config_signal_engine.json" \
  "$APP/user_data/trading_enabled.json" \
  "$APP/user_data/tenants/simeon/trading_enabled.json" \
  "$APP/deploy/cryptotools-signal-engine.service" \
  "$APP/deploy/trade-executor.service" \
  "$APP/deploy/enable-shared-bots.sh"
chown www-data:www-data /var/www/criptotools-ui/app.js
chmod +x "$APP/deploy/enable-shared-bots.sh"
rm -rf "$APP/user_data/strategies/__pycache__"
sudo -u cryptotools "$APP/.venv/bin/python" -m py_compile "$APP/scripts/trade_executor.py" "$APP/scripts/signal_bus.py"
systemctl restart pair-config
bash "$APP/deploy/enable-shared-bots.sh"
sleep 25
systemctl is-active cryptotools-signal-engine trade-executor pair-config
curl -s --max-time 10 -o /dev/null -w "8081:%{http_code}\n" http://127.0.0.1:8081/api/v1/ping
journalctl -u trade-executor -n 5 --no-pager
free -h | head -2
uptime

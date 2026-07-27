#!/bin/bash
set -e
FT=/home/freqtrade/freqtrade
cp /tmp/reconcile_positions.py /tmp/pair_config_server.py "$FT/scripts/"
cp /tmp/index.html /tmp/app.js /tmp/styles.css /var/www/criptotools-ui/
cp /tmp/position-reconcile.service /tmp/position-reconcile.timer /etc/systemd/system/
chown freqtrade:freqtrade "$FT/scripts/reconcile_positions.py" "$FT/scripts/pair_config_server.py"
chown -R www-data:www-data /var/www/criptotools-ui
sudo -u freqtrade "$FT/.venv/bin/python" "$FT/scripts/reconcile_positions.py" --fix --json
systemctl daemon-reload
systemctl restart pair-config
systemctl enable --now position-reconcile.timer
echo "=== after fix ==="
sudo -u freqtrade "$FT/.venv/bin/python" "$FT/scripts/reconcile_positions.py"
systemctl is-active pair-config position-reconcile.timer

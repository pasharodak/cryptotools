#!/bin/bash
set -e
FT=/home/cryptotools/app
cp /tmp/bybit_grid_manager.py /tmp/scan_bybit_grid.py "$FT/scripts/"
cp /tmp/bybit_grid_config.json "$FT/user_data/"
cp /tmp/app.js /tmp/styles.css /var/www/criptotools-ui/
chown cryptotools:cryptotools "$FT/scripts/bybit_grid_manager.py" "$FT/scripts/scan_bybit_grid.py" "$FT/user_data/bybit_grid_config.json"
chown www-data:www-data /var/www/criptotools-ui/app.js /var/www/criptotools-ui/styles.css
systemctl restart pair-config
sudo -u cryptotools "$FT/.venv/bin/python" /tmp/_fund_grid_account.py

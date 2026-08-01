#!/bin/bash
set -euo pipefail
cd /home/cryptotools/app
source /home/cryptotools/.cryptotools.env
.venv/bin/python3 scripts/seed_changelog.py
.venv/bin/python3 scripts/scan_ranging_pairs.py -q
TOKEN=$(curl -s -u "$FREQUI_USERNAME:$FREQUI_PASSWORD" -X POST http://127.0.0.1:8082/api/v1/token/login | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
curl -s -X POST http://127.0.0.1:8082/api/v1/reload_config -H "Authorization: Bearer $TOKEN"
systemctl restart pair-config
python3 -c "import json; d=json.load(open('user_data/ranging_pairs.json')); print('whitelist:', d.get('whitelist'))"
python3 -c "import json; bl=json.load(open('user_data/config_grid.json'))['exchange']['pair_blacklist']; print('blacklist count:', len(bl))"

#!/bin/bash
set -euo pipefail
echo "=== open trades before restart ==="
source /home/freqtrade/.freqtrade.env
TOKEN=$(curl -s -u "$FREQUI_USERNAME:$FREQUI_PASSWORD" -X POST http://127.0.0.1:8080/api/v1/token/login | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
curl -s http://127.0.0.1:8080/api/v1/status -H "Authorization: Bearer $TOKEN" | python3 -c "import sys,json; d=json.load(sys.stdin); print('open', len(d)); [print(t['trade_id'], t['pair'], 'short' if t.get('is_short') else 'long') for t in d]"

echo "=== restart freqtrade (open trades kept in DB) ==="
systemctl daemon-reload
systemctl restart freqtrade
sleep 15
systemctl is-active freqtrade

echo "=== open trades after restart ==="
TOKEN=$(curl -s -u "$FREQUI_USERNAME:$FREQUI_PASSWORD" -X POST http://127.0.0.1:8080/api/v1/token/login | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")
if [ -n "$TOKEN" ]; then
  curl -s http://127.0.0.1:8080/api/v1/status -H "Authorization: Bearer $TOKEN" | python3 -c "import sys,json; d=json.load(sys.stdin); print('open', len(d)); [print(t['trade_id'], t['pair']) for t in d]" || true
fi

echo "=== last log lines ==="
tail -15 /home/freqtrade/freqtrade/user_data/logs/freqtrade-freqai.log 2>/dev/null || journalctl -u freqtrade -n 15 --no-pager

#!/bin/bash
set -e
source /home/cryptotools/.cryptotools.env
API="http://127.0.0.1:8082/api/v1"
TOKEN=$(curl -sk -u "$FREQUI_USERNAME:$FREQUI_PASSWORD" -X POST "$API/token/login" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
AUTH="Authorization: Bearer $TOKEN"

echo "=== state ==="
curl -sk -H "$AUTH" "$API/show_config" | python3 -c "import sys,json; d=json.load(sys.stdin); print('state:', d.get('state'), 'force:', d.get('force_entry_enable'), 'open:', d.get('max_open_trades'))"

echo "=== open trades ==="
curl -sk -H "$AUTH" "$API/status"

echo ""
echo "=== whitelist ==="
curl -sk -H "$AUTH" "$API/whitelist"

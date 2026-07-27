#!/bin/bash
# Force-exit all open trades on a freqtrade bot API (default :8080 FreqAI).
set -euo pipefail
source /home/freqtrade/.freqtrade.env
PORT="${1:-8080}"
API="http://127.0.0.1:${PORT}/api/v1"
TOKEN=$(curl -s -u "$FREQUI_USERNAME:$FREQUI_PASSWORD" -X POST "${API}/token/login" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
OPEN=$(curl -s "${API}/status" -H "Authorization: Bearer $TOKEN")
COUNT=$(echo "$OPEN" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
echo "Port ${PORT}: ${COUNT} open trade(s)"
if [ "$COUNT" -eq 0 ]; then
  exit 0
fi
echo "$OPEN" | python3 -c "
import sys, json
for t in json.load(sys.stdin):
    print(t['trade_id'], t['pair'])
"
curl -s -X POST "${API}/forceexit" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tradeid":"all","ordertype":"market"}' | python3 -m json.tool
sleep 2
curl -s "${API}/status" -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('Remaining open:', len(d))"

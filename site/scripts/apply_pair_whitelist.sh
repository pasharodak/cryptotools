#!/bin/bash
set -euo pipefail
source /home/freqtrade/.freqtrade.env

echo "Restarting bots..."
systemctl restart freqtrade freqtrade-strategy freqtrade-grid
sleep 20

OLD_PAIRS=(
  "ALGO/USDT:USDT"
  "EIGEN/USDT:USDT"
  "TRUMP/USDT:USDT"
  "RESOLV/USDT:USDT"
  "PUMPFUN/USDT:USDT"
)

for port in 8080 8081 8082; do
  TOKEN=$(curl -s -u "$FREQUI_USERNAME:$FREQUI_PASSWORD" -X POST "http://127.0.0.1:${port}/api/v1/token/login" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
  for pair in "${OLD_PAIRS[@]}"; do
    curl -s -X POST "http://127.0.0.1:${port}/api/v1/blacklist" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"blacklist\":[\"$pair\"]}" >/dev/null || true
  done
  echo "=== port $port whitelist ==="
  curl -s "http://127.0.0.1:${port}/api/v1/whitelist" -H "Authorization: Bearer $TOKEN"
  echo
done

systemctl is-active freqtrade freqtrade-strategy freqtrade-grid

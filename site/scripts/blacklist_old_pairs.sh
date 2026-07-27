#!/bin/bash
source /home/freqtrade/.freqtrade.env
OLD=("ALGO/USDT:USDT" "EIGEN/USDT:USDT" "TRUMP/USDT:USDT" "RESOLV/USDT:USDT" "PUMPFUN/USDT:USDT")
TOKEN=$(curl -s -u "$FREQUI_USERNAME:$FREQUI_PASSWORD" -X POST http://127.0.0.1:8080/api/v1/token/login | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
for pair in "${OLD[@]}"; do
  curl -s -X POST http://127.0.0.1:8080/api/v1/blacklist -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"blacklist\":[\"$pair\"]}"
  echo
done
curl -s -X POST http://127.0.0.1:8080/api/v1/reload_config -H "Authorization: Bearer $TOKEN"
echo
curl -s http://127.0.0.1:8080/api/v1/whitelist -H "Authorization: Bearer $TOKEN"
echo

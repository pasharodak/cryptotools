#!/bin/bash
source /home/cryptotools/.cryptotools.env
for port in 8080 8081; do
  TOKEN=$(curl -s -u "$FREQUI_USERNAME:$FREQUI_PASSWORD" -X POST "http://127.0.0.1:${port}/api/v1/token/login" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
  echo "=== port $port ==="
  curl -s "http://127.0.0.1:${port}/api/v1/whitelist" -H "Authorization: Bearer $TOKEN"
  echo
done

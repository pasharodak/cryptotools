#!/bin/bash
source /home/freqtrade/.freqtrade.env
TOKEN=$(curl -s -u "$FREQUI_USERNAME:$FREQUI_PASSWORD" -X POST http://127.0.0.1:8080/api/v1/token/login | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
curl -s http://127.0.0.1:8080/api/v1/status -H "Authorization: Bearer $TOKEN" | python3 -c "import sys,json; d=json.load(sys.stdin); print('open', len(d)); [print(t['trade_id'], t['pair']) for t in d]"

#!/bin/bash
set -e
source /home/freqtrade/.freqtrade.env
PAIR="${1:-SIREN/USDT:USDT}"
SIDE="${2:-long}"
API="http://127.0.0.1:8082/api/v1"
BASE=/home/freqtrade/freqtrade
PY=$BASE/.venv/bin/python3

# ensure running + force entry enabled
$PY - <<'PY'
import json
from pathlib import Path
p = Path("/home/freqtrade/freqtrade/user_data/config_grid.json")
cfg = json.loads(p.read_text())
cfg["force_entry_enable"] = True
cfg["initial_state"] = "running"
p.write_text(json.dumps(cfg, indent=4) + "\n")
PY

systemctl restart freqtrade-grid
sleep 12

TOKEN=$(curl -sk -u "$FREQUI_USERNAME:$FREQUI_PASSWORD" -X POST "$API/token/login" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
AUTH="Authorization: Bearer $TOKEN"

# add pair to whitelist if missing
$PY - <<PY
import json
from pathlib import Path
pair = "$PAIR"
p = Path("/home/freqtrade/freqtrade/user_data/config_grid.json")
cfg = json.loads(p.read_text())
wl = list(cfg.get("exchange", {}).get("pair_whitelist", []))
if pair not in wl:
    wl.insert(0, pair)
    cfg.setdefault("exchange", {})["pair_whitelist"] = wl
    p.write_text(json.dumps(cfg, indent=4) + "\n", encoding="utf-8")
    print("added", pair)
else:
    print("already in wl", pair)
PY

curl -sk -H "$AUTH" -X POST "$API/reload_config"
sleep 5

STATE=$(curl -sk -H "$AUTH" "$API/show_config" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state'))")
echo "state=$STATE"
if [ "$STATE" != "running" ]; then
  curl -sk -H "$AUTH" -X POST "$API/start"
  sleep 5
fi

echo "=== forceenter $PAIR $SIDE ==="
curl -sk -H "$AUTH" -H "Content-Type: application/json" -X POST "$API/forceenter" -d "{\"pair\":\"$PAIR\",\"side\":\"$SIDE\"}"

echo ""
echo "=== status ==="
curl -sk -H "$AUTH" "$API/status"

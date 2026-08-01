#!/usr/bin/env bash
# CryptoTools VPS cutover: migrate from /home/freqtrade/freqtrade -> /home/cryptotools/app
set -euo pipefail
OLD_USER=freqtrade
OLD_APP=/home/freqtrade/freqtrade
OLD_ENV=/home/freqtrade/.freqtrade.env
NEW_USER=cryptotools
NEW_APP=/home/cryptotools/app
NEW_ENV=/home/cryptotools/.cryptotools.env
STAGING=/tmp/cryptotools_release

echo "=== 1. Create user/dirs ==="
id -u "$NEW_USER" &>/dev/null || useradd -m -s /bin/bash "$NEW_USER"
mkdir -p "$NEW_APP" /home/cryptotools
mkdir -p /etc/nginx/ssl

echo "=== 2. Stop old bots ==="
systemctl stop freqtrade-strategy freqtrade-grid pair-config ranging-scanner.service strategy-scanner.service position-reconcile.service 2>/dev/null || true
systemctl stop freqtrade 2>/dev/null || true
systemctl disable --now freqtrade-strategy freqtrade-grid freqtrade pair-config 2>/dev/null || true
systemctl mask freqtrade.service 2>/dev/null || true

echo "=== 3. Unpack new release (code) ==="
if [ ! -f "$STAGING/app.tgz" ]; then
  echo "missing $STAGING/app.tgz" >&2
  exit 1
fi
rm -rf /tmp/ct_unpack
mkdir -p /tmp/ct_unpack "$NEW_APP"
tar -xzf "$STAGING/app.tgz" -C /tmp/ct_unpack
# normalize unpack root
SRC=/tmp/ct_unpack
if [ -d /tmp/ct_unpack/ctengine ]; then SRC=/tmp/ct_unpack; fi
if [ -d /tmp/ct_unpack/site/ctengine ]; then SRC=/tmp/ct_unpack/site; fi
rsync -a --delete \
  --exclude 'user_data' \
  --exclude '.venv' \
  "$SRC/" "$NEW_APP/"

echo "=== 4. Merge user_data: keep live DBs, refresh code artifacts ==="
mkdir -p "$NEW_APP/user_data"
if [ -d "$OLD_APP/user_data" ]; then
  rsync -a "$OLD_APP/user_data/" "$NEW_APP/user_data/"
fi
# overlay release configs/strategies/models/ml (from tarball)
if [ -d "$SRC/user_data" ]; then
  rsync -a \
    --exclude '*.sqlite' --exclude '*.sqlite-*' \
    --exclude 'logs' --exclude 'data' --exclude 'plot' --exclude 'notebooks' \
    "$SRC/user_data/" "$NEW_APP/user_data/"
fi

echo "=== 5. Env file ==="
if [ -f "$OLD_ENV" ]; then
  cp -a "$OLD_ENV" "$NEW_ENV"
elif [ -f "$STAGING/cryptotools.env" ]; then
  cp -a "$STAGING/cryptotools.env" "$NEW_ENV"
fi
# migrate old env var prefixes if present
if [ -f "$NEW_ENV" ]; then
  sed -i 's/FREQTRADE__/CTENGINE__/g' "$NEW_ENV" || true
  sed -i 's/\r$//' "$NEW_ENV" || true
  chmod 600 "$NEW_ENV"
fi

echo "=== 6. bot_limits finder key ==="
python3 - <<'PY'
import json
from pathlib import Path
p = Path("/home/cryptotools/app/user_data/bot_limits.json")
if p.is_file():
    d = json.loads(p.read_text())
    mot = d.get("max_open_trades") or {}
    if "freqai" in mot and "finder" not in mot:
        mot["finder"] = mot.pop("freqai")
    if "freqai" in mot:
        mot.pop("freqai", None)
    stakes = d.get("stake_amount") or {}
    if "freqai" in stakes and "finder" not in stakes:
        stakes["finder"] = stakes.pop("freqai")
    stakes.pop("freqai", None)
    d["max_open_trades"] = mot
    d["stake_amount"] = stakes
    p.write_text(json.dumps(d, indent=2) + "\n")
    print("bot_limits migrated", mot)
else:
    print("no bot_limits yet")
PY

echo "=== 7. venv + install ==="
cd "$NEW_APP"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install -U pip wheel
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install -e .
./.venv/bin/ctbot --version || ./.venv/bin/python -m ctengine --version

echo "=== 8. systemd units ==="
cp -f "$NEW_APP/deploy/cryptotools-strategy.service" /etc/systemd/system/
cp -f "$NEW_APP/deploy/cryptotools-grid.service" /etc/systemd/system/
cp -f "$NEW_APP/deploy/cryptotools-finder.service" /etc/systemd/system/
cp -f "$NEW_APP/deploy/pair-config.service" /etc/systemd/system/
# scanners / reconcile if present
for u in ranging-scanner.service ranging-scanner.timer strategy-scanner.service strategy-scanner.timer position-reconcile.service position-reconcile.timer; do
  [ -f "$NEW_APP/deploy/$u" ] && cp -f "$NEW_APP/deploy/$u" /etc/systemd/system/ || true
done
# fix After= in pair-config
sed -i 's/ctengine\.service/cryptotools-finder.service/g' /etc/systemd/system/pair-config.service || true

echo "=== 9. nginx + TLS ==="
# copy certs to new names if needed
if [ -f /etc/nginx/ssl/freqtrade.crt ] && [ ! -f /etc/nginx/ssl/cryptotools.crt ]; then
  cp -a /etc/nginx/ssl/freqtrade.crt /etc/nginx/ssl/cryptotools.crt
  cp -a /etc/nginx/ssl/freqtrade.key /etc/nginx/ssl/cryptotools.key
fi
cp -f "$NEW_APP/deploy/nginx-cryptotools.conf" /etc/nginx/sites-available/cryptotools
ln -sfn /etc/nginx/sites-available/cryptotools /etc/nginx/sites-enabled/cryptotools
rm -f /etc/nginx/sites-enabled/freqtrade
nginx -t
systemctl reload nginx

echo "=== 10. UI ==="
mkdir -p /var/www/criptotools-ui
cp -f "$NEW_APP/custom-ui/"* /var/www/criptotools-ui/ 2>/dev/null || true
chown -R www-data:www-data /var/www/criptotools-ui || true

echo "=== 11. permissions ==="
chown -R "$NEW_USER:$NEW_USER" /home/cryptotools
chmod 600 "$NEW_ENV" || true

echo "=== 12. enable & start ==="
systemctl daemon-reload
systemctl enable cryptotools-strategy cryptotools-grid pair-config
systemctl restart pair-config
systemctl restart cryptotools-strategy cryptotools-grid
# finder stays disabled by default
systemctl disable --now cryptotools-finder 2>/dev/null || true
systemctl mask cryptotools-finder 2>/dev/null || true

# disable old units permanently
systemctl disable --now freqtrade-strategy freqtrade-grid 2>/dev/null || true
systemctl mask freqtrade-strategy freqtrade-grid freqtrade 2>/dev/null || true

sleep 4
echo "=== status ==="
systemctl is-active cryptotools-strategy cryptotools-grid pair-config nginx || true
curl -sk -o /dev/null -w "strategy:%{http_code}\n" https://127.0.0.1:8443/api/strategy/ping || true
curl -sk -o /dev/null -w "grid:%{http_code}\n" https://127.0.0.1:8443/api/grid/ping || true
curl -sk -o /dev/null -w "finder:%{http_code}\n" https://127.0.0.1:8443/api/finder/ping || true
curl -s -o /dev/null -w "pairconfig:%{http_code}\n" http://127.0.0.1:8090/ || true
echo "CUTOVER_OK"

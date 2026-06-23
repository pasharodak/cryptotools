#!/bin/bash
# Restore network (if wgcf broke routing) + nginx HTTPS for FreqUI on port 8443
set -e
export DEBIAN_FRONTEND=noninteractive

echo "=== 1. Network recovery ==="
wg-quick down wgcf 2>/dev/null || true
systemctl disable --now wgcf-warp 2>/dev/null || true
systemctl disable --now warp-svc 2>/dev/null || true
ip -4 rule del table 51820 2>/dev/null || true
ip -6 rule del table 51820 2>/dev/null || true

echo "=== 2. nginx + SSL ==="
apt-get update -y
apt-get install -y nginx openssl

mkdir -p /etc/nginx/ssl
if [ ! -f /etc/nginx/ssl/freqtrade.crt ]; then
  openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout /etc/nginx/ssl/freqtrade.key \
    -out /etc/nginx/ssl/freqtrade.crt \
    -subj "/CN=freqtrade"
fi

install -m 644 /tmp/nginx-freqtrade.conf /etc/nginx/sites-available/freqtrade
ln -sf /etc/nginx/sites-available/freqtrade /etc/nginx/sites-enabled/freqtrade
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl restart nginx

echo "=== 3. Firewall ==="
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH
  ufw allow 8443/tcp comment 'FreqUI HTTPS'
  ufw --force enable || true
fi

echo "=== 4. Freqtrade API (localhost only) ==="
mkdir -p /etc/systemd/system/freqtrade.service.d
rm -f /etc/systemd/system/freqtrade.service.d/network.conf
rm -f /etc/systemd/system/freqtrade.service.d/warp.conf

systemctl daemon-reload
systemctl restart freqtrade
sleep 8

echo "=== 5. Status ==="
systemctl is-active nginx
systemctl is-active freqtrade
curl -sS --connect-timeout 5 http://127.0.0.1:8080/api/v1/ping || true
echo ""
echo "FreqUI: https://$(curl -sS --connect-timeout 3 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}'):8443"
echo "Login/password: see FREQUI_* in /home/freqtrade/.freqtrade.env"

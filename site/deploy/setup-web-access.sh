#!/bin/bash
# Restore network (if wgcf broke routing) + nginx HTTPS for CriptoTools UI on port 8443
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
if [ ! -f /etc/nginx/ssl/cryptotools.crt ]; then
  openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout /etc/nginx/ssl/cryptotools.key \
    -out /etc/nginx/ssl/cryptotools.crt \
    -subj "/CN=ctengine"
fi

UI_DIR=/var/www/criptotools-ui
mkdir -p "$UI_DIR"
if [ -d /tmp/custom-ui ]; then
  cp /tmp/custom-ui/index.html /tmp/custom-ui/app.js /tmp/custom-ui/styles.css "$UI_DIR/"
fi

install -m 644 /tmp/nginx-cryptotools.conf /etc/nginx/sites-available/cryptotools
ln -sf /etc/nginx/sites-available/cryptotools /etc/nginx/sites-enabled/cryptotools
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl restart nginx

echo "=== 3. Firewall ==="
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH
  ufw allow 8443/tcp comment 'CriptoTools HTTPS'
  ufw --force enable || true
fi

echo "=== 4. Remove bundled FreqUI ==="
FREQUI_DIR=/home/cryptotools/app/ctengine/rpc/api_server/ui/installed
if [ -d "$FREQUI_DIR" ]; then
  find "$FREQUI_DIR" -mindepth 1 -delete
fi

echo "=== 5. Status ==="
systemctl is-active nginx
echo ""
echo "CriptoTools UI: https://$(curl -sS --connect-timeout 3 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}'):8443"
echo "Login/password: see FREQUI_* in /home/cryptotools/.cryptotools.env"

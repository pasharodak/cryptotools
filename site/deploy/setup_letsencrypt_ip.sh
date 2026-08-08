#!/bin/bash
# Install free Let's Encrypt IP certificate for CryptoTools UI (https://IP:8443)
set -euo pipefail

IP="77.222.35.209"
EMAIL="${LETSENCRYPT_EMAIL:-admin@localhost}"
WEBROOT="/var/www/cryptotools-acme"
NGINX_SITE="/etc/nginx/sites-enabled/cryptotools"

echo "=== 1) Install certbot (snap, need >=5.4 for --ip-address) ==="
if ! command -v snap >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y snapd
  systemctl enable --now snapd.socket || true
  sleep 2
fi
snap install core 2>/dev/null || true
snap refresh core 2>/dev/null || true
snap install --classic certbot
ln -sf /snap/bin/certbot /usr/bin/certbot
certbot --version

echo "=== 2) Open HTTP :80 for ACME ==="
ufw allow 80/tcp comment 'ACME HTTP-01' || true
ufw status | head -20

echo "=== 3) ACME webroot + nginx :80 ==="
mkdir -p "$WEBROOT/.well-known/acme-challenge"
chown -R www-data:www-data /var/www/cryptotools-acme

cat >/etc/nginx/sites-available/acme-http <<'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    root /var/www/cryptotools-acme;
    location /.well-known/acme-challenge/ {
        default_type text/plain;
        allow all;
    }
    location / {
        return 301 https://$host:8443$request_uri;
    }
}
EOF
ln -sfn /etc/nginx/sites-available/acme-http /etc/nginx/sites-enabled/acme-http
nginx -t
systemctl reload nginx

echo "=== 4) Issue Let's Encrypt IP cert (shortlived ~6d) ==="
# Backup self-signed
mkdir -p /etc/nginx/ssl/backup
cp -a /etc/nginx/ssl/cryptotools.crt /etc/nginx/ssl/backup/ 2>/dev/null || true
cp -a /etc/nginx/ssl/cryptotools.key /etc/nginx/ssl/backup/ 2>/dev/null || true

# Prefer real email from env file if present
if [ -f /home/cryptotools/.cryptotools.env ]; then
  # shellcheck disable=SC1091
  set +u
  # no secrets printed
  set -u
fi

certbot certonly \
  --webroot \
  --webroot-path "$WEBROOT" \
  --preferred-profile shortlived \
  --ip-address "$IP" \
  --agree-tos \
  --register-unsafely-without-email \
  --non-interactive \
  --keep-until-expiring \
  --deploy-hook "systemctl reload nginx"

LIVE="/etc/letsencrypt/live/$IP"
if [ ! -f "$LIVE/fullchain.pem" ]; then
  # some certbot versions use underscore or different dirname
  LIVE=$(find /etc/letsencrypt/live -maxdepth 1 -mindepth 1 -type d | head -1)
fi
echo "LIVE=$LIVE"
ls -la "$LIVE"

echo "=== 5) Point nginx :8443 to LE cert ==="
cp -a "$NGINX_SITE" "${NGINX_SITE}.bak.$(date +%Y%m%d%H%M%S)"
# Replace ssl_certificate lines
sed -i "s|ssl_certificate     .*|ssl_certificate     $LIVE/fullchain.pem;|" "$NGINX_SITE"
sed -i "s|ssl_certificate_key .*|ssl_certificate_key $LIVE/privkey.pem;|" "$NGINX_SITE"
# Also set server_name to IP for clarity
sed -i "s|server_name .*;|server_name $IP;|" "$NGINX_SITE"
grep -E "ssl_certificate|server_name|listen" "$NGINX_SITE"
nginx -t
systemctl reload nginx

echo "=== 6) Ensure frequent renewal (6-day certs) ==="
# Snap certbot already has a timer; force twice-daily + renew early
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat >/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh <<'EOF'
#!/bin/bash
systemctl reload nginx
EOF
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

# Override renew timer to run every 12h (shortlived needs ~every 2-3 days)
systemctl enable --now snap.certbot.renew.timer 2>/dev/null || true
# Extra timer as safety net every 12 hours
cat >/etc/systemd/system/cryptotools-le-renew.service <<EOF
[Unit]
Description=Renew Let's Encrypt shortlived IP cert for CryptoTools
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/certbot renew --quiet --deploy-hook /bin/systemctl\\ reload\\ nginx
EOF
cat >/etc/systemd/system/cryptotools-le-renew.timer <<'EOF'
[Unit]
Description=Twice-daily LE renew for CryptoTools IP cert

[Timer]
OnCalendar=*-*-* 03,15:17:00
Persistent=true
RandomizedDelaySec=600

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now cryptotools-le-renew.timer

echo "=== 7) Verify ==="
echo | openssl s_client -connect 127.0.0.1:8443 -servername "$IP" 2>/dev/null | openssl x509 -noout -subject -issuer -dates
curl -sS -o /dev/null -w "https_local:%{http_code}\n" --resolve "$IP:8443:127.0.0.1" "https://$IP:8443/" || true
echo "DONE. Open https://$IP:8443 (cert renews automatically, ~6 day lifetime)."

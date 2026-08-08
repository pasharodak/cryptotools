# Настройка VPS — CryptoTools (Bybit)

Стек: **3 бота** + **pair-config** + **custom-ui** + **ML entry-gate**.  
ОС: Ubuntu 22.04/24.04/26.04 LTS. Рекомендуется ≥4 CPU / 6 GB RAM.

Корень приложения на сервере: `/home/cryptotools/app` (= содержимое `site/`).  
Linux-пользователь: `cryptotools`. Секреты: `/home/cryptotools/.cryptotools.env`.

---

## Сервисы

| systemd unit | Роль | Порт API |
|--------------|------|----------|
| `cryptotools-strategy` | Strategy (`MultiStrategyRouter`) | 8081 |
| `cryptotools-grid` | Grid (`VolatilityGridStrategy`) | 8082 |
| `cryptotools-finder` | ML Finder (`TradeFinderStrategy`, обычно off) | 8080 |
| `pair-config` | whitelist / лимиты / UI API | 8090 |
| nginx | UI + прокси `:8443` | — |

UI API: `/api/finder/`, `/api/strategy/`, `/api/grid/`, `/api/pair-config/`.

Strategy risk: **SL −15%**, **ROI +5%**.

Первый реbrand с legacy: `site/scripts/deploy_cutover_rebrand.ps1`.

---

## Деплой с ПК

```powershell
cd D:\cryptotools\site\scripts
.\deploy_prod_ml.ps1
```

Секреты: `site/.env` → на VPS `~/.cryptotools.env`.  
SSH-ключ: `site/deploy/id_rsa/`.

```bash
sudo cp /home/cryptotools/app/deploy/nginx-cryptotools.conf /etc/nginx/sites-available/cryptotools
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl restart pair-config cryptotools-strategy cryptotools-grid
```

---

## Новый VPS (кратко)

```bash
apt update && apt upgrade -y
adduser cryptotools && usermod -aG sudo cryptotools
ufw allow OpenSSH && ufw allow 8443/tcp && ufw enable
timedatectl set-timezone UTC && timedatectl set-ntp true
```

Дальше — `deploy_prod_ml.ps1` / скрипты в `deploy/`.

---

## Проверка

```bash
systemctl is-active cryptotools-strategy cryptotools-grid pair-config
curl -sk https://127.0.0.1:8443/api/strategy/ping
curl -sk https://127.0.0.1:8443/api/pair-config/state
```

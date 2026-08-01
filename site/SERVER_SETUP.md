# Настройка VPS — CryptoTools (Bybit)

Стек: **3 Freqtrade-бота** + **pair-config** + **custom-ui** + **ML entry-gate**.  
ОС: Ubuntu 22.04/24.04/26.04 LTS. Рекомендуется ≥4 CPU / 6 GB RAM.

Корень на сервере: `/home/freqtrade/freqtrade` (= содержимое `site/`).

---

## Сервисы

| systemd | Роль | Порт API |
|---------|------|----------|
| `freqtrade-strategy` | `MultiStrategyRouter` | 8081 |
| `freqtrade-grid` | `VolatilityGridStrategy` | 8082 |
| `freqtrade` | `TradeFinderStrategy` (ML Finder) | 8080 |
| `pair-config` | whitelist / лимиты / UI API | 8090 |
| nginx | UI + прокси `:8443` | — |

UI API:
- `/api/finder/` → :8080 (legacy alias `/api/freqai/` тоже работает)
- `/api/strategy/` → :8081
- `/api/grid/` → :8082
- `/api/pair-config/` → :8090

Strategy risk (актуально): **SL −15%**, **ROI +5%**.

---

## Деплой с ПК

```powershell
cd D:\cryptotools\site\scripts
.\deploy_prod_ml.ps1
```

Секреты: `site/.env` → на VPS `~/.freqtrade.env`.  
SSH-ключ: `site/deploy/id_rsa/`.

После смены nginx/UI:

```bash
sudo cp /home/freqtrade/freqtrade/deploy/nginx-freqtrade.conf /etc/nginx/sites-available/freqtrade
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl restart pair-config freqtrade-strategy freqtrade-grid
# Finder по желанию:
# sudo systemctl start freqtrade
```

Юниты: `site/deploy/*.service`, таймеры сканеров, `disable-finder-bot.sh` если Finder выключен.

---

## Базовая безопасность (новый VPS)

```bash
apt update && apt upgrade -y
adduser freqtrade
usermod -aG sudo freqtrade
ufw allow OpenSSH
ufw allow 8443/tcp
ufw enable
timedatectl set-timezone UTC
timedatectl set-ntp true
```

Python venv и зависимости — через `deploy_prod_ml.ps1` / `SERVER` скрипты в `deploy/`.  
Классический upstream FreqAI **не используется** (не нужен `pip install -e ".[freqai]"` для прода).

---

## Проверка

```bash
systemctl is-active freqtrade-strategy freqtrade-grid pair-config
curl -sk https://127.0.0.1:8443/api/strategy/ping
curl -sk https://127.0.0.1:8443/api/pair-config/state
```

Подробности продукта: [`README.md`](README.md) в корне `cryptotools`, UI: `custom-ui/`.

# Настройка VPS — CryptoTools (Bybit)

Стек: **shared Strategy** (signal-engine + trade-executor) + **Grid** + **ML Finder** + **pair-config** + **custom-ui** + ML entry-gate.  
ОС: **Ubuntu 24.04/26.04 LTS** (22.04 тоже ок). Ориентир: ≥3 CPU / **6 GB RAM** (с Finder — лучше 8+).

Корень приложения: `/home/cryptotools/app` (= содержимое `site/`).  
Linux-пользователь: `cryptotools`. Секреты: `/home/cryptotools/.cryptotools.env`.

---

## Сервисы

| systemd unit | Роль | Порт API |
|--------------|------|----------|
| `cryptotools-signal-engine` | Сигналы `MultiStrategyRouter` (`CT_SIGNAL_ONLY`) | 8081 |
| `trade-executor` | Исполнение сигналов на ключах пользователя | — |
| `cryptotools-grid` | Grid (`VolatilityGridStrategy`) | 8082 |
| `cryptotools-finder` | ML Finder (`TradeFinderStrategy`, barrier XF) | 8080 |
| `pair-config` | whitelist / лимиты / UI API / reconcile / control-plane | 8090 |
| `cryptotools-telegram-bot` | Telegram Mini App + уведомления | — |
| nginx | UI + прокси `:8443` | — |

Legacy unit `cryptotools-strategy` может оставаться как fallback; продовый путь — **signal-engine → trade-executor**.

UI API: `/api/finder/`, `/api/strategy/`, `/api/grid/`, `/api/pair-config/`.

Strategy risk (основные): **SL −15%**, **ROI +5%**.  
Тестовый блок: `user_data/test_strategy_settings.json`.

Сверка позиций с Bybit: UI «Синхронизировать» + auto в pair-config (~5 мин).  
Cross-bot first-wins: `scripts/pair_entry_guard.py`.

---

## Деплой с ПК

```powershell
cd D:\cryptotools\site\scripts
.\deploy_prod_ml.ps1
```

Секреты: `site/.env` → на VPS `~/.cryptotools.env`.  
SSH-ключ: `site/deploy/id_rsa/` (в git не коммитить).

```bash
sudo cp /home/cryptotools/app/deploy/nginx-cryptotools.conf /etc/nginx/sites-available/cryptotools
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl restart pair-config cryptotools-signal-engine trade-executor cryptotools-grid
# Finder — по необходимости:
# sudo systemctl restart cryptotools-finder
```

Юниты: `deploy/cryptotools-signal-engine.service`, `deploy/trade-executor.service`, `deploy/enable-shared-bots.sh`.

---

## Новый VPS (кратко)

```bash
apt update && apt upgrade -y
adduser cryptotools && usermod -aG sudo cryptotools
ufw allow OpenSSH && ufw allow 8443/tcp && ufw enable
timedatectl set-timezone UTC && timedatectl set-ntp true
```

Дальше — `deploy_prod_ml.ps1` / скрипты в `deploy/`.  
Ресурсы: см. таблицу в корневом [`README.md`](../README.md).

---

## Проверка

```bash
systemctl is-active cryptotools-signal-engine trade-executor cryptotools-grid pair-config
curl -sk https://127.0.0.1:8443/api/strategy/ping
curl -sk https://127.0.0.1:8443/api/pair-config/state
```

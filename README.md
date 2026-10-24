# CryptoTools

Торговый стек для **Bybit USDT Perpetual**: live-боты, веб-панель, multi-user (Live/Demo), ML entry-gate, симуляции и обучение.

| | |
|--|--|
| **Прод (сейчас)** | Contabo `194.163.149.122` → UI [https://194.163.149.122:8443/](https://194.163.149.122:8443/) |
| **Код прода** | `site/` ≈ `/home/cryptotools/app` на VPS |
| **UI на сервере** | `/var/www/criptotools-ui/` |
| **Симуляции** | `simulation/` (offline; live сами не трогают) |
| **Для AI** | [`AGENTS.md`](AGENTS.md) — читай перед правками |

Старый VPS `77.222.35.209` **не используется**. Все деплои и SSH — только на Contabo.

---

## Что это

На одном Bybit-счёте (или tenant-счётах) крутятся:

1. **Strategy** — shared `signal-engine` (`MultiStrategyRouter`) пишет сигналы → `trade-executor` исполняет на ключах пользователя.
2. **Grid** — `VolatilityGridStrategy`.
3. **ML Finder** — `TradeFinderStrategy` (barrier Transformer).
4. **pair-config** — whitelist, лимиты, Start/Stop (control-plane), JWT/auth, reconcile vs Bybit.
5. **custom-ui** — панель за nginx `:8443`.

```
Browser → HTTPS :8443 (nginx)
           ├── static UI  (/var/www/criptotools-ui)
           ├── /api/pair-config/*  → :8090
           ├── /api/strategy/*     → :8081  (signal-engine)
           ├── /api/grid/*         → :8082
           └── /api/finder/*       → :8080
```

| Unit | Config / код | Порт |
|------|--------------|------|
| `cryptotools-signal-engine` | `config_signal_engine.json` | 8081 |
| `trade-executor` | `scripts/trade_executor.py` | — |
| `cryptotools-grid` | `config_grid.json` | 8082 |
| `cryptotools-finder` | `config.json` | 8080 |
| `pair-config` | `pair_config_server.py` | 8090 |
| `cryptotools-telegram-bot` | `telegram_bot.py` | — |

Секреты на VPS: `/home/cryptotools/.cryptotools.env`. SSH: `site/deploy/id_rsa/id_rsa`, user `root`.  
Если SSH/SCP таймаутится — **выключите VPN**. Скрипты деплоя в репозитории не публикуются (см. раздел «Деплой»).

---

## Правила работы

1. **`site/` = прод, `simulation/` = офлайн.** Не заливайте sim-конфиги на Contabo «молча»; синхрон ML/флагов — только через `apply_prod_ml_config.py` + деплой.
2. **Не коммитить секреты:** `.env`, API-ключи, `deploy/id_rsa/`.
3. **Не править `site/ctengine/`** без явной нужды.
4. **Стратегии:** логика в `simulation/strategies/`, в проде — тонкие wrappers в `site/user_data/strategies/`.
5. **Один Strategy-бот** = `MultiStrategyRouter` + shared executor (tenant’ы не поднимают отдельный strategy-unit).
6. **Start/Stop в UI** пишет `desired` в `control_plane.sqlite`; `bot_reconcile` поднимает процессы через:
   - admin: `deploy/admin-systemctl.sh` (на сервере, NOPASSWD sudoers);
   - tenant grid/finder: `deploy/tenant-systemctl.sh` (на сервере).
7. **Настройки пользователя** (enabled, placement, test limits) — в `control_plane.sqlite` (`user_settings`), не только JSON.
8. **Коммиты / push** — только по просьбе. **Не выдумывать PnL** — смотреть отчёты/логи.
9. **Деплой Contabo** — VPN off; ключ `site/deploy/id_rsa`; хост `194.163.149.122`; деплой-скрипты хранятся вне репозитория.
10. **Cross-bot:** `pair_entry_guard` (first-wins) + UI «Синхронизировать» / `reconcile_positions.py`.

Подробности для агента: [`AGENTS.md`](AGENTS.md). Сервер: [`site/SERVER_SETUP.md`](site/SERVER_SETUP.md).

---

## Multi-user (кратко)

| Роль | Как |
|------|-----|
| **Admin** | Shared units + `.cryptotools.env` |
| **Live** | `users.json` + `secrets/{id}.enc` + `tenants/{id}/` |
| **Demo** | Пара `username_demo` (api-demo Bybit), переключатель Live↔Demo в UI |

Логин: `POST /api/pair-config/auth/login`. Боты пользователя: `PATCH /api/pair-config/bots/{strategy|grid|finder}`.

---

## Локально

```powershell
cd D:\cryptotools\site
.\scripts\load_env.ps1
.\scripts\start_local_site.ps1   # UI :8443 + pair-config

# Shared Strategy (осторожно — нужен .env)
$env:CT_SIGNAL_ONLY = "1"
.\.venv\Scripts\ctbot.exe trade --config user_data\config_signal_engine.json `
  --strategy MultiStrategyRouter --strategy-path user_data\strategies
.\.venv\Scripts\python.exe scripts\trade_executor.py
```

Симы:

```powershell
cd D:\cryptotools\simulation\scripts
.\start_player.ps1 -SkipDownload
```

---

## Деплой на Contabo

> В опубликованный репозиторий деплой-инструменты не входят: `site/deploy/*` и `deploy_*.ps1` удалены в `f74b321`.
> На сервере остаются установленные systemd-юниты, nginx-конфиг и sudo-wrappers. Деплой выполняется приватным набором скриптов владельца — не ищите эти файлы локально.

SSH (VPN off): `ssh -i site/deploy/id_rsa/id_rsa root@194.163.149.122`.

Проверка на сервере:

```bash
systemctl is-active cryptotools-signal-engine trade-executor cryptotools-grid cryptotools-finder pair-config
journalctl -u pair-config -n 50 --no-pager
journalctl -u cryptotools-finder -n 40 --no-pager
```

---

## Структура

```
cryptotools/
├── README.md / AGENTS.md
├── site/                 # прод ≈ Contabo app/
│   ├── custom-ui/
│   ├── scripts/          # pair-config, executor, reconcile
│   ├── deploy/           # локально только id_rsa; systemd/nginx/wrappers — на сервере
│   ├── user_data/        # configs, strategies, ml, models
│   └── ctengine/         # движок (не трогать без нужды)
└── simulation/           # offline + ML train
```

CLI: `ctbot` / пакет `ctengine`. Бренд в UI: **CryptoTools**.

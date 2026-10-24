# AGENTS.md — CryptoTools

Контекст для AI. Читай **в начале сессии**, прежде чем менять код.

| | |
|--|--|
| **Корень** | `D:\cryptotools` |
| **Прод** | Contabo **`194.163.149.122`** · app `/home/cryptotools/app` · UI `/var/www/criptotools-ui` · [https://194.163.149.122:8443/](https://194.163.149.122:8443/) |
| **Симы** | `simulation/` — offline; сами live не переписывают |
| **Старый VPS** | `77.222.35.209` — **не трогать**, не деплоить |

---

## 1. Проект

Стек **CryptoTools** для **Bybit USDT Perpetual**:

| Слой | Где | Зачем |
|------|-----|-------|
| Live | `site/` | signal-engine + executor + Grid + Finder |
| UI | `site/custom-ui/` → VPS `/var/www/criptotools-ui` | панель `:8443` |
| Control API | `pair_config_server.py` | whitelist, JWT, control-plane, reconcile |
| ML gate | `user_data/ml/` + models | `confirm_trade_entry` |
| Симы / train | `simulation/` | replay, player, обучение |

Движок: `ctengine` / CLI `ctbot`, env `CTENGINE__*`. Пакет `site/ctengine/` **не править без запроса**.

---

## 2. Структура

```
cryptotools/
├── AGENTS.md / README.md
├── site/                     ← прод ≈ Contabo
│   ├── ctengine/
│   ├── custom-ui/
│   ├── deploy/               # локально только id_rsa; systemd/nginx/wrappers — на сервере
│   ├── scripts/              # pair-config, executor, guard, reconcile
│   └── user_data/            # configs, strategies, ml, models, tenants/, control_plane.sqlite
└── simulation/
    ├── config/               # player_scenarios, prod_ml_bots, prod_top30_pack
    ├── strategies/           # источник логики стратегий
    ├── scripts/              # train_*, apply_prod_ml_config, compare
    └── results/
```

Локально `user_data/` / `.venv/` в корне могут быть junction → `site/`.

---

## 3. Железные правила

1. **`site/` = прод, `simulation/` = офлайн.** Синхрон в live — явно (`apply_prod_ml_config.py` + деплой).
2. **Не коммитить секреты** (`.env`, ключи, `deploy/id_rsa/`).
3. **Не править `ctengine/`** без запроса.
4. Стратегии: логика в `simulation/strategies/`, wrappers в `site/user_data/strategies/`.
5. Strategy = **`MultiStrategyRouter`** + **shared** `trade-executor` (нет per-tenant strategy unit).
6. Коммиты / push / force — только по просьбе пользователя.
7. **SSH только Contabo** `194.163.149.122`, ключ `site/deploy/id_rsa/id_rsa`, user `root`. VPN часто ломает SSH — при VPN off. Деплой-скрипты хранятся вне репозитория (удалены в `f74b321`).
8. Не выдумывать PnL — отчёты `simulation/results/**` или journalctl на VPS.
9. Admin Start/Stop systemd: через **`deploy/admin-systemctl.sh`** + sudoers (`sudoers-cryptotools-tenants`) — файлы на сервере, в git их нет. Tenant grid/finder: **`tenant-systemctl.sh`**. Plain `systemctl` от user `cryptotools` → `Interactive authentication required`.
10. Настройки UI (enabled / placement / test limits / **max_open_trades / stake** / **strategy whitelist**): **`control_plane.sqlite` → `user_settings`** (ключ `bot_limits` и др.); JSON-конфиги — write-through для ctbot. UI и trade-executor **не** читают лимиты из live `show_config`. Shared signal-engine всегда `max_open_trades=1`. Скан стратегий **не** затирает большой StaticPairList (`apply_whitelist=false`, shrink-guard).

---

## 4. Прод-сервисы (Contabo)

| systemd | Config | Роль | API |
|---------|--------|------|-----|
| `cryptotools-signal-engine` | `config_signal_engine.json` | MultiStrategyRouter (`CT_SIGNAL_ONLY`) | `:8081` |
| `trade-executor` | `trade_executor.py` | сигналы → Bybit по юзеру | — |
| `cryptotools-grid` | `config_grid.json` | VolatilityGridStrategy | `:8082` |
| `cryptotools-finder` | `config.json` | TradeFinderStrategy | `:8080` |
| `pair-config` | `pair_config_server.py` | UI API, control-plane | `:8090` |
| `cryptotools-telegram-bot` | `telegram_bot.py` | Mini App + уведомления | — |

Продовый путь Strategy: **signal-engine → signal_bus → trade-executor**.  
Legacy `cryptotools-strategy` + `config_strategy.json` — не основной путь.

### Guard / reconcile

- `pair_entry_guard.py` — first-wins по паре (Finder / Strategy / Grid).
- `reconcile_positions.py` — призраки и дубли; UI «Синхронизировать» + auto ~5 мин.

### Multi-user

- **Admin** — shared units + `.cryptotools.env`.
- **Live** — `users.json` (`account_kind: live`), `secrets/{id}.enc`, `tenants/{id}/`, порты `18100+`.
- **Demo** — `username_demo` (`account_kind: demo`), api-demo Bybit; switch Live↔Demo в UI.
- Логин JWT → `bot-proxy/{bot}/...`. Placement / enabled / test settings — **per user** в SQLite.
- Новые users: боты OFF по умолчанию; Live без блока `test_strategies` (если не выдали права).
- Sudoers: `deploy/sudoers-cryptotools-tenants` (tenant + admin wrappers). Master: `SECRETS_MASTER_KEY`.

### Control-plane

- DB: `user_data/control_plane.sqlite` (desired / observed / outbox / user_settings).
- API: `GET|PATCH /bots/{strategy|grid|finder}`, SSE `/bots/events`.
- UI пишет **desired**; worker `bot_reconcile` поднимает process + sync `trading_enabled`.

### Telegram

- Unit `cryptotools-telegram-bot`; Mini App URL → Contabo `:8443`.
- Chat id на Live/parent; Demo через `telegram_owner_id`.

### Деплой

> Деплой-инструменты (`site/deploy/*`, `deploy_*.ps1`, `fix_finder_start_auth.bat`) в репозиторий не входят — удалены в `f74b321`. На сервере остаются установленные units, nginx-конфиг и sudo-wrappers. Деплой — приватным набором скриптов владельца. SSH: `site/deploy/id_rsa/id_rsa`, VPN off.

`enabled_strategies.json` / `bot_strategies.json` **не заливаются** слепым scp как source of truth с ПК: перед apply — pull с VPS; на сервере merge только дописывает новые id (OFF).

---

## 5. Strategy architecture

1. Enabled set → `MultiStrategyRouter` вызывает саб-стратегии.
2. `enter_tag` = id (или `:inv` / `:hedge`).
3. Mean-rev → ADX-cap; вход → ML gate по `SCENARIO_BY_TAG`.
4. Выход: ROI/SL роутера; per-tag через `TAG_RISK` (`custom_stoploss` / `custom_roi`).
5. Глобальный fallback: SL **−15%**, ROI **+5%**.
6. UI-панели: **Тестовые** → **Стратегии** → Grid → Bybit Grid → Finder. Placement: promote/demote (per user). Каталог: `AVAILABLE_STRATEGIES` в pair-config (`id`, `num`, `ui_order`, `test_group`).
7. Актуальный pack / сценарии: `simulation/config/prod_top30_pack.json`, `prod_ml_bots.json`, `enabled` на VPS в DB/JSON — **не копировать устаревшие списки #1–116 из старых чатов**.

### Добавить стратегию в прод

1. Логика + scenario в `simulation/`.
2. Wrapper в `site/user_data/strategies/`.
3. Registry / `SCENARIO_BY_TAG` / `TAG_RISK` в `MultiStrategyRouter`.
4. Каталог UI + pack/`apply_prod_ml_config.py` / `prod_ml_bots.json`.
5. Деплой Contabo (VPN off).

---

## 6. Симуляции и ML

| Что | Где |
|-----|-----|
| Сценарии | `simulation/config/player_scenarios.json` |
| Прод-набор ML | `simulation/config/prod_ml_bots.json` |
| Train / apply | `simulation/scripts/train_*.py`, `apply_prod_ml_config.py` |
| Player | `:18999` — `start_player.ps1` |

В player/backtest per-scenario SL/TP должны попадать в конфиг сессии.

---

## 7. Именование

| Было | Сейчас |
|------|--------|
| freqtrade | ctengine / ctbot |
| `FREQTRADE__*` | `CTENGINE__*` |
| старый VPS 77.222… | **Contabo 194.163.149.122** |

---

## 8. Типичные задачи

| Задача | Куда |
|--------|------|
| Start/Stop бота | UI → control-plane; admin unit через `admin-systemctl.sh` (на сервере) |
| Вкл стратегию | UI / user_settings / enabled на VPS |
| Деплой | приватный деплой-набор владельца (в репозитории отсутствует) |
| Логи | `journalctl -u cryptotools-*-n 80` |
| Sync позиций | UI «Синхронизировать» |
| Finder не стартует (auth) | проверить sudoers/wrapper на сервере (батник в репо не входит) |

---

## 9. НЕ делать

- Не деплоить на `77.222.35.209`.
- Не запускать live локально без просьбы.
- Не `push --force`, не менять git config.
- Не править balance/маржу костылями в стратегиях без запроса.
- Не переименовывать `enter_tag` без миграции открытых сделок.
- Не полагаться на устаревшие README/чаты про «6 стратегий» или старый IP.

---

## 10. Быстрые команды

```powershell
# Local UI
cd D:\cryptotools\site
.\scripts\load_env.ps1
.\scripts\start_local_site.ps1

# Deploy Contabo: приватный набор скриптов (в репозитории отсутствует)
```

```bash
# Contabo
systemctl is-active cryptotools-signal-engine trade-executor cryptotools-grid cryptotools-finder pair-config
journalctl -u cryptotools-finder -n 40 --no-pager
```

---

## 11. Поддержка документа

При смене прода (IP, стек, control-plane, деплой) — обнови **этот файл и корневой README** в той же сессии.  
Ориентир: **2026-10-06** · прод = Contabo `194.163.149.122`.

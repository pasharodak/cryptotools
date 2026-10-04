# CryptoTools

Торговый стек для **Bybit USDT Perpetual**: live-боты, панель управления, ML entry-gate, симуляции и обучение моделей.

**Репозиторий:** `D:\cryptotools`.  
**Для AI/агента:** сначала [`AGENTS.md`](AGENTS.md) — структура, правила прода vs симов, как добавлять стратегии.

| Папка | Назначение |
|-------|------------|
| `site/` | Прод: боты, UI, pair-config, shared stack, деплой на VPS |
| `simulation/` | Офлайн: симы, replay, свипы, обучение ML |
| `user_data/`, `.venv/` | Junction → `site/` (локально; в git не входят) |

---

## Архитектура прода

```
Browser (custom-ui)
        │  HTTPS :8443  (локально: gateway :8443)
        ▼
     nginx / local_site_gateway
        ├── static UI
        ├── /api/pair-config/*  → pair_config :8090
        ├── /api/strategy/*     → signal-engine :8081  (shared stack)
        ├── /api/grid/*         → Grid         :8082
        └── /api/finder/*       → ML Finder    :8080

pair-config ──► control-plane, whitelist, лимиты, reconcile vs Bybit
signal-engine ──► MultiStrategyRouter ──► signal_bus ──► trade-executor ──► Bybit
ML Finder     ──► barrier Transformer (или XGBoost) + optional LightGBM gate
Grid          ──► VolatilityGridStrategy
```

| Роль | Config / unit | Стратегия / процесс | Порт |
|------|---------------|---------------------|------|
| **Strategy (signals)** | `config_signal_engine.json` / `cryptotools-signal-engine` | `MultiStrategyRouter` (signal-only) | 8081 |
| **Executor** | `trade-executor.service` | `scripts/trade_executor.py` | — |
| **Grid** | `config_grid.json` | `VolatilityGridStrategy` | 8082 |
| **ML Finder** | `config.json` | `TradeFinderStrategy` (barrier XF) | 8080 |
| **pair-config** | — | `pair_config_server.py` | 8090 |

Strategy risk (основные): **SL −15%**, **ROI +5%**.  
Тестовый блок: свои лимиты в `test_strategy_settings.json` (max open / per / stake / SL·TP); потолок Strategy-бота поднимается до тестового max.

### Защита от дублей на одном счёте Bybit

- `scripts/pair_entry_guard.py` — first-wins между Finder / Strategy / Grid (БД ботов + Bybit).
- UI «Синхронизировать» + auto-reconcile (~5 мин) — призраки и дубли пар (`reconcile_positions.py`).

---

## Быстрый старт (локально)

```powershell
cd D:\cryptotools\site
.\scripts\load_env.ps1

# Панель + pair-config (без обязательного старта ботов)
.\scripts\start_local_site.ps1

# Shared Strategy stack
$env:CT_SIGNAL_ONLY = "1"
.\.venv\Scripts\ctbot.exe trade --config user_data\config_signal_engine.json --strategy MultiStrategyRouter --strategy-path user_data\strategies
.\.venv\Scripts\python.exe scripts\trade_executor.py
```

Секреты: `site/.env`. Деплой/VPS: [`site/SERVER_SETUP.md`](site/SERVER_SETUP.md).

### Симуляции и обучение

```powershell
cd D:\cryptotools\simulation\scripts
.\run_sim_replay.ps1
.\start_player.ps1 -SkipDownload
python train_pnl_classifier.py
python train_trade_finder.py
python train_eval_barrier_transformer.py   # barrier XF для Finder
```

Подробнее: [`simulation/README.md`](simulation/README.md).

---

## VPS (ориентир)

| | Минимум | Комфорт |
|--|---------|---------|
| ОС | **Ubuntu 24.04/26.04 LTS** | то же |
| CPU | 3× ~3.6 ГГц | 4+ vCPU |
| RAM | **6 GB** (без Finder можно 4 GB впритык) | 8–12 GB |
| Диск | 40 GB NVMe | 80 GB+ |

С Finder (PyTorch) запас RAM важнее числа ядер. Обучение ML — не на том же VPS, что live.

---

## Структура

```
cryptotools/
├── site/                 # прод ≈ VPS
│   ├── custom-ui/
│   ├── scripts/          # pair-config, executor, guard, reconcile, deploy
│   ├── deploy/           # systemd, nginx
│   ├── user_data/        # configs, strategies, ml/, models/
│   └── ctengine/         # торговый движок (не править без нужды)
└── simulation/           # симы + обучение ML
```

CLI/пакет движка: `ctengine` / `ctbot`. В продукте и UI — бренд **CryptoTools**.

# CryptoTools

Торговый стек на **Freqtrade** + **Bybit USDT Perpetual**: live-боты, панель управления, ML entry-gate, симуляции и обучение моделей.

**Единственный репозиторий:** `D:\cryptotools`.

| Папка | Назначение |
|-------|------------|
| `site/` | Прод: боты, UI, pair-config, деплой на VPS |
| `simulation/` | Офлайн: симы, replay, свипы, **обучение ML** |
| `user_data/`, `.venv/` | Junction → `site/` (локально; в git не входят) |

---

## Архитектура прода

```
Browser (custom-ui)
        │  HTTPS :8443
        ▼
     nginx (VPS)
        ├── static UI
        ├── /api/pair-config/*  → pair_config_server :8090
        ├── /api/strategy/*     → Strategy :8081
        ├── /api/grid/*         → Grid     :8082
        └── /api/finder/*       → Finder   :8080

pair-config ──► config_*.json / bot_limits / enabled_strategies → reload ботам
ML gate     ──► confirm_trade_entry (strategy / grid / finder)
Боты        ──► Bybit USDT Perp
```

| Бот | Config | Стратегия | Порт |
|-----|--------|-----------|------|
| **Strategy** | `config_strategy.json` | `MultiStrategyRouter` | 8081 |
| **Grid** | `config_grid.json` | `VolatilityGridStrategy` | 8082 |
| **ML Finder** | `config.json` | `TradeFinderStrategy` | 8080 |

Strategy risk: **SL −15%**, **ROI +5%** (`minimal_roi["0"]`).

Источник правок: `site/` → деплой на VPS. UI управляет через pair-config.

---

## Быстрый старт

### Прод (локально / деплой)

```powershell
cd D:\cryptotools\site
.\scripts\load_env.ps1
.\.venv\Scripts\freqtrade.exe trade --config user_data\config_strategy.json --strategy MultiStrategyRouter

cd D:\cryptotools\site\scripts
.\deploy_prod_ml.ps1
```

Секреты: `site/.env`. SSH: `site/deploy/id_rsa/`.  
VPS: [`site/SERVER_SETUP.md`](site/SERVER_SETUP.md).

### Симуляции и обучение моделей

```powershell
cd D:\cryptotools\simulation\scripts
.\run_sim_replay.ps1
.\start_player.ps1 -SkipDownload

# обучение gate / finder (см. simulation/README.md)
python train_pnl_classifier.py
python train_trade_finder.py
```

Данные: `simulation/data/`, `simulation/results/` (gitignored).  
Модели после обучения → `site/user_data/models/` → деплой.

---

## Структура

```
cryptotools/
├── README.md
├── site/                         # ≈ VPS /home/freqtrade/freqtrade
│   ├── custom-ui/                # CriptoTools panel
│   ├── scripts/                  # pair-config, scanners, deploy
│   ├── deploy/                   # systemd, nginx
│   ├── user_data/
│   │   ├── config.json           # Finder
│   │   ├── config_strategy.json
│   │   ├── config_grid.json
│   │   ├── strategies/           # MultiStrategyRouter, Grid, TradeFinder, …
│   │   ├── ml/                   # entry gate
│   │   └── models/               # pnl_classifier, trade_finder
│   └── freqtrade/                # vendored upstream package
└── simulation/                   # полный офлайн-контур
    ├── config/
    ├── ml/                       # обучение / gate logic
    ├── models/
    ├── scripts/                  # train_*, backtest_*, sweep_*, …
    ├── strategies/
    ├── exchange_sim/ player/
    ├── data/  results/
    └── README.md
```

---

## Что убрано

Классический **FreqAI** (LightGBMRegressor / FreqaiExample / старые start_*.ps1 и модели `criptotools-bybit-*`) удалён.  
В проде только **TradeFinder + pnl_classifier gate**. Вендорный пакет `site/freqtrade/` (включая upstream-модуль freqai) не трогаем — это зависимости Freqtrade, в live-конфигах FreqAI выключен.

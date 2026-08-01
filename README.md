# CryptoTools

Торговый стек для **Bybit USDT Perpetual**: live-боты, панель управления, ML entry-gate, симуляции и обучение моделей.

**Репозиторий:** `D:\cryptotools`.

| Папка | Назначение |
|-------|------------|
| `site/` | Прод: боты, UI, pair-config, деплой на VPS |
| `simulation/` | Офлайн: симы, replay, свипы, обучение ML |
| `user_data/`, `.venv/` | Junction → `site/` (локально; в git не входят) |

---

## Архитектура прода

```
Browser (custom-ui)
        │  HTTPS :8443
        ▼
     nginx (VPS)
        ├── static UI
        ├── /api/pair-config/*  → pair_config :8090
        ├── /api/strategy/*     → Strategy    :8081
        ├── /api/grid/*         → Grid        :8082
        └── /api/finder/*       → ML Finder   :8080

pair-config ──► config_*.json / bot_limits / enabled_strategies → reload
ML gate     ──► confirm_trade_entry
Боты        ──► Bybit USDT Perp
```

| Бот | Config | Стратегия | Порт |
|-----|--------|-----------|------|
| **Strategy** | `config_strategy.json` | `MultiStrategyRouter` | 8081 |
| **Grid** | `config_grid.json` | `VolatilityGridStrategy` | 8082 |
| **ML Finder** | `config.json` | `TradeFinderStrategy` | 8080 |

Strategy risk: **SL −15%**, **ROI +5%**.

---

## Быстрый старт

```powershell
cd D:\cryptotools\site
.\scripts\load_env.ps1
.\.venv\Scripts\ctbot.exe trade --config user_data\config_strategy.json --strategy MultiStrategyRouter

cd D:\cryptotools\site\scripts
.\deploy_prod_ml.ps1
```

Секреты: `site/.env`. Деплой/VPS: [`site/SERVER_SETUP.md`](site/SERVER_SETUP.md).

### Симуляции и обучение

```powershell
cd D:\cryptotools\simulation\scripts
.\run_sim_replay.ps1
.\start_player.ps1 -SkipDownload
python train_pnl_classifier.py
python train_trade_finder.py
```

Подробнее: [`simulation/README.md`](simulation/README.md).

---

## Структура

```
cryptotools/
├── site/                 # прод ≈ VPS
│   ├── custom-ui/
│   ├── scripts/          # pair-config, scanners, deploy
│   ├── deploy/           # systemd, nginx
│   ├── user_data/        # configs, strategies, ml/, models/
│   └── ctengine/        # внутренний торговый движок (не править без нужды)
└── simulation/           # симы + обучение ML
```

CLI и пакет движка на диске называются `ctengine` (историческое имя upstream) — в продукте и UI используется бренд **CryptoTools / CriptoTools**.

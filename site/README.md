# CryptoTools — site (прод)

Live на Bybit USDT Perp: **Strategy** + **Grid** + **ML Finder**, панель **custom-ui**, control plane **pair-config**, фильтр входов **ML gate**.

Симуляции и обучение моделей: `../simulation/` (не смешивать с продом).

## Боты

| Unit | Config | Стратегия | Порт | UI API |
|------|--------|-----------|------|--------|
| `freqtrade-strategy` | `config_strategy.json` | `MultiStrategyRouter` | 8081 | `/api/strategy/` |
| `freqtrade-grid` | `config_grid.json` | `VolatilityGridStrategy` | 8082 | `/api/grid/` |
| `freqtrade` | `config.json` | `TradeFinderStrategy` | 8080 | `/api/finder/` |

pair-config: `:8090` → `/api/pair-config/`.  
Strategy: **SL −15%**, **ROI `{"0": 0.05}`**.

## Локально

```powershell
cd D:\cryptotools\site
.\scripts\load_env.ps1
.\.venv\Scripts\freqtrade.exe trade --config user_data\config_strategy.json --strategy MultiStrategyRouter
```

## Деплой

```powershell
cd D:\cryptotools\site\scripts
.\deploy_prod_ml.ps1
```

См. [`SERVER_SETUP.md`](SERVER_SETUP.md).

## Структура

```
site/
├── custom-ui/           # CriptoTools panel
├── scripts/             # pair-config, scanners, deploy
├── deploy/              # systemd, nginx
├── user_data/
│   ├── config*.json
│   ├── strategies/      # router, grid, finder, sub-strategies
│   ├── ml/              # entry gate
│   └── models/          # pnl_classifier, trade_finder
└── freqtrade/           # vendored Freqtrade (upstream; не править без нужды)
```

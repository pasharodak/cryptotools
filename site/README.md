# CryptoTools — site (прод)

Live на Bybit USDT Perp: **Strategy** + **Grid** + **ML Finder**, панель **custom-ui**, **pair-config**, **ML gate**.

Симуляции: `../simulation/`.

## Боты

| Unit (VPS) | Config | Стратегия | Порт | UI API |
|------------|--------|-----------|------|--------|
| strategy | `config_strategy.json` | `MultiStrategyRouter` | 8081 | `/api/strategy/` |
| grid | `config_grid.json` | `VolatilityGridStrategy` | 8082 | `/api/grid/` |
| finder | `config.json` | `TradeFinderStrategy` | 8080 | `/api/finder/` |

systemd-имена на сервере: `cryptotools-strategy`, `cryptotools-grid`, `ctengine` (исторические имена unit-файлов).

Strategy: **SL −15%**, **ROI `{"0": 0.05}`**.

## Локально

```powershell
cd D:\cryptotools\site
.\scripts\load_env.ps1
.\.venv\Scripts\ctbot.exe trade --config user_data\config_strategy.json --strategy MultiStrategyRouter
```

## Деплой

```powershell
cd D:\cryptotools\site\scripts
.\deploy_prod_ml.ps1
```

См. [`SERVER_SETUP.md`](SERVER_SETUP.md).

# CryptoTools

Торговые боты на **Freqtrade** + **Bybit USDT Perpetual**, веб-панель и симулятор.

| Папка | Назначение |
|-------|------------|
| `site/` | Прод: стратегии, ML-gate, custom UI, деплой на VPS |
| `simulation/` | Симулятор биржи, replay, обучение ML-моделей |

Корень `user_data/` и `.venv/` — junction на `site/` (удобные ярлыки на ПК, в git не входят).

## Прод (site)

```powershell
cd D:\cryptotools\site
.\scripts\load_env.ps1
.\.venv\Scripts\freqtrade.exe trade --config user_data\config_strategy.json --strategy MultiStrategyRouter
```

Деплой на VPS:

```powershell
cd D:\cryptotools\site\scripts
.\deploy_prod_ml.ps1
```

Секреты: `site/.env` (не в git). SSH-ключ: `site/deploy/id_rsa/` (не в git).

Подробнее: [`site/README.md`](site/README.md), [`site/SERVER_SETUP.md`](site/SERVER_SETUP.md).

## Симуляция

```powershell
cd D:\cryptotools\simulation\scripts
.\run_sim_replay.ps1
```

Sim Player (replay 1s):

```powershell
cd D:\cryptotools\simulation\scripts
.\start_player.ps1 -SkipDownload
```

Свечи и результаты (`simulation/data/`, `simulation/results/`) в git не коммитятся (~1.4 GB+).

Подробнее: [`simulation/README.md`](simulation/README.md).

## Структура site (прод)

```
site/
├── custom-ui/          # веб-панель CriptoTools
├── scripts/            # сканеры, API, деплой
├── deploy/             # systemd, nginx
├── user_data/
│   ├── strategies/     # MultiStrategyRouter, Grid, Lite*, TradeFinder…
│   ├── ml/             # entry gate, features
│   └── models/         # pnl_classifier, trade_finder
└── freqtrade/          # код Freqtrade (форк/копия)
```

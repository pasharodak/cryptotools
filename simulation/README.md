# Симулятор биржи и контрфактический replay

Изолированная среда для ответа на вопрос: **какой баланс был бы, если бы улучшения внедрили сразу?**

Живой VPS и прод-конфиги в `site/user_data/config*.json` **не трогаются**.

## Архитектура

```
D:\cryptotools\
├── simulation\
│   ├── exchange_sim\     # симулятор биржи (свечи + виртуальный кошелёк)
│   ├── integration\      # экспорт сделок, download-data, counterfactual
│   ├── config\           # manifest + backtest-конфиги (dry_run)
│   ├── ml\               # обучение gate / trade_finder
│   ├── models\           # обученные классификаторы по сценариям
│   ├── player\           # UI плеера
│   ├── strategies\       # sim-only стратегии
│   ├── data\             # SQLite с VPS, свечи (gitignored)
│   └── results\          # отчёты (gitignored)
└── site\                 # прод (не меняется симуляцией)
```

### Режимы

| Режим | Назначение |
|-------|------------|
| **Backtest replay** | Backtesting на истории из live SQLite |
| **Exchange Sim API** | `http://127.0.0.1:18999` — kline, wallet, order/create |
| **Sim Player UI** | тот же порт — график 1s, плеер, ускорение ×2–×3600 |

## Sim Player (1s replay)

```powershell
cd D:\cryptotools\simulation\scripts

# Загрузить 1s-свечи из архива Bybit
.\download_player_data.ps1

# Запустить плеер (API + UI)
.\start_player.ps1 -SkipDownload
```

Пары по умолчанию: SOL, AVAX, XRP, HEI, SEI, DOGE, LINK, ADA, SUI, NEAR (`manifest.json` → `player_pairs`).

Sim-tuning: стратегии в `simulation/strategies/` — прод-боты не меняются. Отчёт: `results/player_stats.json`, настройки: `config/sim_tuning.json`.

## Быстрый старт (counterfactual)

```powershell
cd D:\cryptotools\simulation\scripts

# Полный прогон: VPS DB → свечи → baseline vs improved
.\run_sim_replay.ps1

# Только локально (если DB уже скачаны)
.\run_sim_replay.ps1 -SkipFetch
```

## Что сравнивается

| Сценарий | Бот | Параметры |
|----------|-----|-----------|
| `grid_baseline` | Grid FT | ADX 28, DCA, SL −5%, stake 5 |
| `grid_improved` | Grid FT | ADX 22, без DCA, SL −3%, stake 3, blacklist |
| `strategy_baseline` | Стратегии | CriptoPairs + Supertrend |
| `strategy_improved` | Стратегии | CriptoPairs + BB+RSI |

Стартовый баланс и период — в `simulation/config/manifest.json`.

## Результат

`simulation/results/counterfactual_report.json`:

- фактический PnL с VPS
- гипотетический баланс baseline / improved
- дельта «если бы улучшили сразу»

## Exchange Simulator API

```powershell
cd D:\cryptotools
.\site\.venv\Scripts\pip.exe install -r simulation\requirements.txt
.\site\.venv\Scripts\python.exe simulation\exchange_sim\server.py
```

```http
POST /v5/market/time/set  {"timestamp_ms": 1719300000000}
GET  /v5/market/kline?symbol=SOLUSDT&interval=5
GET  /sim/snapshot
POST /v5/order/create      {"symbol":"SOLUSDT","side":"Buy","qty":0.1}
```

## Безопасность

- Все sim-конфиги: `"dry_run": true`, `"api_server": {"enabled": false}`
- Live DB копируется **только чтением** с VPS в `simulation/data/live_dbs/`
- `simulation/.env.sim` помечает sim-режим (не коммитить секреты)

# CryptoTools — site (прод)

Live на Bybit USDT Perp: **shared Strategy stack** + **Grid** + **ML Finder**, панель **custom-ui**, **pair-config**, ML gate, сверка позиций с биржей.

Симуляции: `../simulation/`. Корневой обзор: [`../README.md`](../README.md).

## Сервисы

| Unit (VPS) | Config / код | Роль | Порт |
|------------|--------------|------|------|
| `cryptotools-signal-engine` | `config_signal_engine.json` | Сигналы `MultiStrategyRouter` (`CT_SIGNAL_ONLY`) | 8081 |
| `trade-executor` | `scripts/trade_executor.py` | Исполнение сигналов на ключах пользователя | — |
| `cryptotools-grid` | `config_grid.json` | `VolatilityGridStrategy` | 8082 |
| `cryptotools-finder` | `config.json` | `TradeFinderStrategy` (barrier Transformer) | 8080 |
| `pair-config` | `scripts/pair_config_server.py` | UI API, control-plane, reconcile | 8090 |
| nginx | `deploy/nginx-cryptotools.conf` | UI + API proxy | 8443 |

UI API: `/api/finder/`, `/api/strategy/`, `/api/grid/`, `/api/pair-config/`.

Strategy risk (основные): **SL −15%**, **ROI `{"0": 0.05}`**.  
Тестовые стратегии: `user_data/test_strategy_settings.json` — свои max open / stake / SL·TP; при max test > Strategy bot — потолок бота поднимается автоматически.

## Локально

```powershell
cd D:\cryptotools\site
.\scripts\load_env.ps1
.\scripts\start_local_site.ps1          # UI :8443 + pair-config :8090

# Shared strategy (сигналы → executor)
$env:CT_SIGNAL_ONLY = "1"
.\.venv\Scripts\ctbot.exe trade --config user_data\config_signal_engine.json `
  --strategy MultiStrategyRouter --strategy-path user_data\strategies
.\.venv\Scripts\python.exe scripts\trade_executor.py
```

## Важные скрипты

| Скрипт | Назначение |
|--------|------------|
| `pair_entry_guard.py` | First-wins блокировка пары между ботами (один счёт Bybit) |
| `reconcile_positions.py` | Призраки / дубли vs Bybit; UI «Синхронизировать» |
| `control_plane.py` | Desired/observed Start/Stop |
| `signal_bus.py` | Очередь сигналов signal-engine → executor |
| `bot_reconcile.py` | Подъём/сверка процессов ботов |

Auto position-reconcile в pair-config: каждые **~5 мин** (env `CT_POSITION_RECONCILE_SEC`).

## Деплой

```powershell
cd D:\cryptotools\site\scripts
.\deploy_prod_ml.ps1
```

См. [`SERVER_SETUP.md`](SERVER_SETUP.md).

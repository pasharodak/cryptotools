# Миграция с Kronos-бота на Freqtrade

## Что изменилось

| Было (criptotools) | Стало (Freqtrade) |
|---|---|
| Kronos ML-прогноз | Техническая стратегия RSI + EMA |
| Ручной выбор сигнала после скана | Автоматические входы/выходы |
| Long spot + Short linear | Long и Short на **Bybit USDT Perpetual** |
| Свой Telegram UI | Встроенный Telegram Freqtrade (`/status`, `/profit`, …) |

## Запуск

```powershell
# 1. Остановить старый Kronos-бот (если работает)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'criptotools\\main\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# 2. Dry-run (без реальных денег) — по умолчанию в config.json
cd D:\freqtrade
.\scripts\load_env.ps1
.\.venv\Scripts\freqtrade.exe trade --config user_data\config.json --strategy CriptoPairsStrategy
```

Секреты подтягиваются из `D:\criptotools\.env` (Bybit + Telegram).

## Полезные команды

```powershell
# Скачать историю для backtest
.\.venv\Scripts\freqtrade.exe download-data --config user_data\config.json --timeframe 1h --days 60

# Backtest
.\.venv\Scripts\freqtrade.exe backtesting --config user_data\config.json --strategy CriptoPairsStrategy

# Web UI: http://127.0.0.1:8080  (login: freqtrader / freqtrader)
```

## Переход на live

1. Прогнать backtest и dry-run несколько дней.
2. В `user_data/config.json` поставить `"dry_run": false`.
3. На Bybit: отдельный subaccount для бота, API с правами **Contract Orders/Positions**.
4. Рекомендуется `"stoploss_on_exchange": true` (уже включено).

## FreqAI (ML вместо Kronos)

Включён **FreqAI** + **LightGBMRegressor**: модель учится на истории (RSI, EMA, Bollinger и др.) и предсказывает движение цены на 12 свечей вперёд (12h на TF 1h).

```powershell
cd D:\freqtrade
.\scripts\start_freqai.ps1
```

Модели сохраняются в `user_data/models/criptotools-bybit-v1/`. Первый запуск **долго обучает** каждую пару (10 шт.) — смотри лог `user_data\logs\stderr.log`.

Backtest:
```powershell
.\.venv\Scripts\freqtrade.exe backtesting --config user_data\config.json --strategy FreqaiExampleStrategy --freqaimodel LightGBMRegressor --timerange 20250501-
```

Другие модели: `freqtrade list-freqaimodels` (XGBoostRegressor, LightGBMClassifier, …).


Используется **тот же** бот-токен, что и у Kronos-бота. Одновременно оба бота с одним токеном работать не могут — держите только Freqtrade.

Команды: `/start`, `/stop`, `/status`, `/profit`, `/balance`, `/help`.

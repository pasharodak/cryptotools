# Freqtrade + FreqAI (Bybit)

Telegram-бот для автоторговли на **Bybit USDT Perpetual** с машинным обучением (**FreqAI**, модель LightGBM).

- Биржа: **Bybit mainnet** (futures, isolated)
- Стратегия: `FreqaiExampleStrategy`
- ML-модель: `LightGBMRegressor`
- Секреты: подтягиваются из `D:\cryptotools\site\.env`

---

## Быстрый старт

### 1. Проверь `.env`

Файл `D:\cryptotools\site\.env` должен содержать:

```env
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
BYBIT_TESTNET=false
TELEGRAM_BOT_TOKEN=...
TRADE_OWNER_USER_ID=...   # ваш Telegram user id
```

На Bybit у API-ключа должны быть права **Contract → Orders** и **Positions** (без вывода средств).

### 2. Запуск

**Live (реальные деньги):**

```powershell
cd D:\cryptotools\site
.\scripts\start_live.ps1
```

**Dry-run (бумажная торговля, без риска):**

В `user_data\config.json` поставь `"dry_run": true`, затем:

```powershell
cd D:\cryptotools\site
.\scripts\start_freqai.ps1
```

### 3. Web UI

Открой в браузере: **http://127.0.0.1:8080**

| Логин | Пароль |
|-------|--------|
| `freqtrader` | `freqtrader` |

Если UI пустой:

```powershell
cd D:\cryptotools\site
.\.venv\Scripts\freqtrade.exe install-ui
```

Перезапусти бота и обнови страницу (Ctrl+F5).

### 4. Telegram

Используется **тот же** бот, что и в `criptotools`. Одновременно может работать **только один** процесс с этим токеном.

Основные команды:

| Команда | Описание |
|---------|----------|
| `/status` | Открытые сделки |
| `/balance` | Баланс |
| `/profit` | Прибыль |
| `/start` | Запустить торговлю |
| `/stop` | Остановить новые входы |
| `/logs` | Хвост лога |
| `/help` | Все команды |

---

## Текущие настройки

Файл: `user_data\config.json`

| Параметр | Значение | Смысл |
|----------|----------|-------|
| `dry_run` | `false` | **Live** — реальные USDT |
| `stake_amount` | `5` | Маржа на сделку (мин. Bybit ~5 USDT) |
| `max_open_trades` | `1` | Одна позиция одновременно |
| `timeframe` | `5m` | Проверка каждые 5 минут |
| `stoploss` | `-0.08` | Стоп −8% |
| Плечо | `3×` | В стратегии (`leverage()`) |

**Пары:** XRP, DOGE, SOL, ADA, ALGO (`USDT:USDT` perpetual).

**FreqAI:**

| Параметр | Значение |
|----------|----------|
| Таймфреймы фич | 5m, 15m, 1h, 4h |
| Горизонт прогноза | 144 свечи × 5m ≈ **12 часов** |
| Переобучение | каждые **6 часов** |
| ID модели | `criptotools-bybit-live-v1` |

**Вход в сделку** (если модель уверена):

- Long: прогноз **> +0.8%**
- Short: прогноз **< −0.8%**

Если в логе только `heartbeat` — бот работает, но **сигнал слабый**, сделки нет.

---

## Логи

| Файл | Назначение |
|------|------------|
| `user_data\logs\live-stderr.log` | Live-бот (фоновый запуск) |
| `user_data\logs\freqtrade-live.log` | Основной лог live |
| `user_data\logs\stderr.log` | Dry-run / старые запуски |

Смотреть в реальном времени:

```powershell
Get-Content D:\cryptotools\site\user_data\logs\live-stderr.log -Wait -Tail 40
```

**Что искать в логе:**

| Строка | Значение |
|--------|----------|
| `Done training ...` | Модель обучена |
| `inferencing pairlist` | Считает прогнозы |
| `dropped ... due to NaNs` | Нормально (~4% строк без индикаторов) |
| `Creating new trade` | Открывает сделку |
| `heartbeat ... RUNNING` | Бот жив |
| `10002` / `InvalidNonce` | Рассинхрон часов Windows |

Модели ML: `user_data\models\criptotools-bybit-live-v1\`

---

## Остановка бота

**Telegram:** `/stop` — не открывает новые сделки.

**Полная остановка процесса:**

```powershell
Get-CimInstance Win32_Process -Filter "Name='freqtrade.exe'" |
  Where-Object { $_.CommandLine -match 'freqtrade\\' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Или через диспетчер задач — процесс `freqtrade.exe`.

---

## Backtest (история, без денег)

```powershell
cd D:\cryptotools\site
.\scripts\load_env.ps1

# Скачать данные (если нужно)
.\.venv\Scripts\freqtrade.exe download-data `
  --config user_data\config.json `
  --timeframe 5m 15m 1h 4h --days 45

# Прогон
.\.venv\Scripts\freqtrade.exe backtesting `
  --config user_data\config.json `
  --strategy FreqaiExampleStrategy `
  --freqaimodel LightGBMRegressor `
  --timerange 20250501-
```

---

## Полезные команды

```powershell
cd D:\cryptotools\site
.\scripts\load_env.ps1

# Список стратегий
.\.venv\Scripts\freqtrade.exe list-strategies --userdir user_data

# Список ML-моделей
.\.venv\Scripts\freqtrade.exe list-freqaimodels

# Проверка конфига
.\.venv\Scripts\freqtrade.exe show-config --config user_data\config.json
```

---

## Смена режима: live ↔ dry-run

1. Открой `user_data\config.json`
2. `"dry_run": true` — бумажная торговля, `"dry_run": false` — реальные деньги
3. Для dry-run можно добавить `"dry_run_wallet": 500`
4. Перезапусти бота

---

## Настройка под себя

### Больше / меньше сделок

Файл: `user_data\strategies\FreqaiExampleStrategy.py`

```python
# В populate_entry_trend — пороги входа (сейчас 0.008 = 0.8%)
df["&-s_close"] > 0.008   # long
df["&-s_close"] < -0.008  # short
```

Меньше порог → больше сделок, выше риск.

### Другая сумма сделки

В `config.json`: `"stake_amount": 5` (не ниже ~5 USDT на Bybit futures).

### Другие пары

В `config.json` → `exchange.pair_whitelist`. После смены пар смени `freqai.identifier` (например на `v2`), чтобы переобучить модели.

### Другая ML-модель

```powershell
freqtrade trade ... --freqaimodel XGBoostRegressor
```

Доступные: `freqtrade list-freqaimodels`

---

## Частые проблемы

### Bybit `10002` / InvalidNonce

Часы Windows опережают сервер Bybit.

1. Параметры уже в конфиге: `recvWindow: 60000`, `adjustForTimeDifference: true`
2. Синхронизируй время: **Параметры → Дата и время → Синхронизировать**
3. Перезапусти бота

### Telegram не отвечает / Conflict

Запущено **несколько** ботов с одним токеном. Оставь один `freqtrade.exe`.

### UI: «Freqtrade UI not installed»

```powershell
cd D:\cryptotools\site
.\.venv\Scripts\freqtrade.exe install-ui
```

Перезапуск бота.

### Бот не торгует

1. Модели обучены? В логе есть `Done training` для всех пар
2. Есть сигнал? Нужен прогноз ±0.8% и `do_predict == 1`
3. Достаточно USDT? Минимум ~5 USDT + запас на комиссии
4. `/status` в Telegram — открыта ли уже 1 сделка (`max_open_trades: 1`)

### Где смотреть на Bybit

**Торговать → Деривативы → USDT Perpetual → Позиции** (не Spot).

---

## Структура проекта

```
D:\cryptotools\site\
├── .venv\                    # Python окружение
├── user_data\
│   ├── config.json           # Главный конфиг
│   ├── strategies\
│   │   └── FreqaiExampleStrategy.py
│   ├── models\               # Обученные FreqAI модели
│   ├── data\bybit\           # Исторические свечи
│   └── logs\                 # Логи
├── scripts\
│   ├── load_env.ps1          # Загрузка секретов из site\.env
│   ├── start_live.ps1        # Live торговля
│   └── start_freqai.ps1      # Dry-run / FreqAI
└── README.md                 # Этот файл
```

---

## Риски

- Торговля на **futures** с плечом может быстро уменьшить депозит.
- FreqAI — не гарантия прибыли; модель ошибается.
- При ~18 USDT одна неудачная сделка заметна.
- Не включай вывод средств на API-ключе.
- Сначала тестируй на `"dry_run": true`.

---

## Ссылки

- [Документация Freqtrade](https://www.freqtrade.io/en/stable/)
- [FreqAI](https://www.freqtrade.io/en/stable/freqai/)
- [Bybit в Freqtrade](https://www.freqtrade.io/en/stable/exchanges/#bybit)

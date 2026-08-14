# AGENTS.md — CryptoTools

Контекст для AI/агента. Читай этот файл **в начале сессии**, прежде чем менять код.
Не нужен полный лог прошлых чатов — здесь зафиксированы суть, структура и правила.

**Корень:** `D:\cryptotools`  
**Прод (live):** `site/` → VPS `77.222.35.209` `/home/cryptotools/app`  
**Симуляции (offline):** `simulation/` — **не трогают** live-конфиги сами по себе.

---

## 1. Что это за проект

Торговый стек **CryptoTools** для **Bybit USDT Perpetual**:

| Слой | Где | Зачем |
|------|-----|-------|
| Live-боты | `site/` | Strategy + Grid (+ Finder обычно off) на VPS |
| UI | `site/custom-ui/` | Панель за nginx `:8443` |
| Pair-config | `site/scripts/pair_config_server.py` | whitelist, лимиты, enable стратегий |
| ML gate | `site/user_data/ml/` + модели | Фильтр входа (`confirm_trade_entry`) |
| Симы / ML train | `simulation/` | Replay, player, сравнение стратегий, обучение |

Движок — форк/ребренд **freqtrade → `ctengine` / CLI `ctbot`**.  
Env-префикс: `CTENGINE__*`. Пакет `site/ctengine/` **не править без явной нужды**.

---

## 2. Структура репозитория

```
cryptotools/
├── AGENTS.md                 ← этот файл
├── README.md
├── site/                     ← прод ≈ содержимое VPS app/
│   ├── ctengine/             # торговый движок (upstream-like)
│   ├── custom-ui/            # веб-UI
│   ├── deploy/               # systemd, nginx, SSH key id_rsa/
│   ├── scripts/              # pair-config, scanners, deploy_*.ps1
│   ├── user_data/
│   │   ├── config_strategy.json / config_grid.json / config.json
│   │   ├── enabled_strategies.json   # какие саб-стратегии ON
│   │   ├── bot_strategies.json       # snapshot для restore после деплоя
│   │   ├── ml_entry_gate.json
│   │   ├── strategies/               # MultiStrategyRouter + wrappers
│   │   ├── ml/                       # gate code
│   │   └── models/                   # pnl_classifier, trade_finder
│   └── .env                  # секреты (не коммитить)
└── simulation/
    ├── config/               # player_scenarios, prod_ml_bots, backtest_*
    ├── strategies/           # sim-only классы (источник логики)
    ├── exchange_sim/         # виртуальная биржа
    ├── player/               # UI плеера :18999
    ├── ml/                   # обучение
    ├── models/               # артефакты моделей
    ├── data/                 # свечи (часто ctengine/), gitignored
    ├── results/              # отчёты сравнений
    └── scripts/              # train_*, run_*_compare, apply_prod_ml_config
```

Локально `user_data/` / `.venv/` в корне могут быть junction → `site/` (в git не входят).

---

## 3. Железные правила

1. **`site/` = прод, `simulation/` = офлайн.** Симы не должны молча переписывать live-торговлю. Прод-флаги/модели синхронятся явно (`apply_prod_ml_config.py` + `deploy_prod_ml.ps1`).
2. **Не коммитить секреты:** `.env`, ключи API, `deploy/id_rsa/`.
3. **Не править `site/ctengine/`** без запроса — только стратегии, UI, scripts, configs, simulation.
4. **Стратегии в проде** — тонкие wrappers в `site/user_data/strategies/`; логика живёт в `simulation/strategies/`.
5. **Один strategy-бот** = `MultiStrategyRouter`. Сигналы саб-стратегий мержатся; выходы/риск — на роутере (+ per-tag risk).
6. **Коммиты / push / force** — только по явной просьбе пользователя.
7. **SSH на VPS:** Git `ssh.exe` + ключ `site/deploy/id_rsa/id_rsa`, user `root` (или deploy-скрипты).
8. **Не выдумывать PnL** — смотреть `simulation/results/**/report.json` или логи VPS.

---

## 4. Прод: боты и сервисы

| systemd | Config | Стратегия | API |
|---------|--------|-----------|-----|
| `cryptotools-strategy` | `config_strategy.json` | `MultiStrategyRouter` | `:8081` → `/api/strategy/` |
| `cryptotools-grid` | `config_grid.json` | `VolatilityGridStrategy` | `:8082` → `/api/grid/` |
| `cryptotools-finder` | `config.json` | `TradeFinderStrategy` | `:8080` (обычно **disabled/masked**) |
| `pair-config` | — | `pair_config_server.py` | `:8090` → `/api/pair-config/` |

### Multi-user (tenants)

- **Admin** = текущий `FREQUI_*` + `.cryptotools.env` Bybit + unit’ы выше.
- **User** = запись в `user_data/users.json`, секреты `user_data/secrets/{id}.enc`, стек в `user_data/tenants/{id}/`, порты из пула `18100+`, systemd `cryptotools-{strategy,grid,finder}@{id}`.
- UI логин → `POST /api/pair-config/auth/login` (JWT). Вызовы ботов → `/api/pair-config/bot-proxy/{bot}/...`.
- Новые users: дефолт `max_open_trades=1` на категорию и Bybit `max_active_bots=1`; потолок как у админа (50 / 5).
- **Права пользователя** (`allowed_strategies` / `allowed_blocks` в `users.json`): админ в Настройки→Пользователи→«Права». `null` = всё; список = только выбранное. Блоки: `test_strategies`, `strategy`, `grid`, `bybitgrid`, `finder`, `history`, `dashboard`, `rating`.
- **Войти как** → `POST /auth/impersonate`; возврат → `POST /auth/stop-impersonate` (JWT claim `imp_by`).
- Sudoers: `deploy/sudoers-cryptotools-tenants`. Master key: `SECRETS_MASTER_KEY` в `.cryptotools.env`.

UI: HTTPS **`:8443`**. Linux user на VPS: `cryptotools`. Секреты: `/home/cryptotools/.cryptotools.env`.

### Деплой

```powershell
cd D:\cryptotools\site\scripts
.\deploy_prod_ml.ps1
```

Скрипт: `apply_prod_ml_config.py` → scp configs/strategies/models/simulation → restart strategy+grid+pair-config.

Подробнее: `site/SERVER_SETUP.md`, `site/README.md`.

---

## 5. Strategy architecture (критично)

### Поток

1. `enabled_strategies.json` → какие id включены.
2. `MultiStrategyRouter.populate_entry_trend` вызывает каждую enabled саб-стратегию.
3. `enter_tag` = id стратегии (или `:inv` / `:hedge`).
4. Mean-rev теги режутся ADX-cap (`MEAN_REV_ADX_TAGS`, порог ~25).
5. `confirm_trade_entry` → ML gate (`allow_trade_entry`) по `SCENARIO_BY_TAG`.
6. Выход: ROI / stoploss роутера; для top-4 — **per-tag** через `custom_stoploss` / `custom_roi` (`TAG_RISK`).

### Глобальный risk роутера (остальные стратегии)

- Hard max SL: **−15%** (`PROD_STRATEGY_STOPLOSS` / config)
- Default ROI: **+5%** (`{"0": 0.05}`)

### Per-tag risk (`TAG_RISK` в MultiStrategyRouter)

Для всех стратегий из `prod_top30_pack.json` (включая #32–39 legacy) — **SL + полный `minimal_roi`**. `custom_stoploss` / `custom_roi` (ступенчатый ROI). Без записи в `TAG_RISK` → глобальные −15% / +5%.

### Паттерн prod wrapper

```python
# site/user_data/strategies/FooStrategy.py
from _sim_live import SimLiveCooldownMixin  # + SimLiveSlTpMixin если нужен прод SL/TP на классе
from simulation.strategies.SomeModule import FooStrategy as _Sim

class FooStrategy(SimLiveCooldownMixin, _Sim):
    pass
```

Для стратегий с **своим** SL/TP в `TAG_RISK` — **без** `SimLiveSlTpMixin` (иначе класс перетрёт sim-значения; роутер всё равно применяет TAG_RISK).

### Как добавить стратегию в прод

1. Логика / бэктест в `simulation/strategies/` + сценарий в `player_scenarios.json` (`stoploss`, `minimal_roi`).
2. Wrapper в `site/user_data/strategies/X.py`.
3. В `MultiStrategyRouter.py`: `STRATEGY_REGISTRY`, `SCENARIO_BY_TAG`, при необходимости `TAG_RISK`.
4. Включить в `enabled_strategies.json` + `_sim_map`; зеркало в `bot_strategies.json`.
5. Обновить `simulation/scripts/apply_prod_ml_config.py` (`_strategy_config`) и `simulation/config/prod_ml_bots.json` (`enabled_scenarios`), иначе следующий деплой **сотрёт** флаги.
6. Каталог UI: `AVAILABLE_STRATEGIES` в `pair_config_server.py` — поля **`id`** (стабильный class/`enter_tag`), **`num`** (номер, не менять при add/remove), **`ui_order`** (сортировка в UI), **`name`** (без `#N` в строке), опционально **`test_group: true`** (блок «Тестовые стратегии» **сверху** панели, отдельно от основных; те же тумблеры, тот же router). Не удалять id из каталога — только `enabled=false`, иначе пропадут лейблы в истории/статах (сделки в БД остаются).
7. `deploy_prod_ml.ps1` — добавить scp модели `by_scenario/{scenario_id}` в список.
8. Залить `simulation/strategies` (деплой уже копирует папку).

`apply_prod_ml_config.py` синхронизирует каталог/модели из pack, но **сохраняет** UI-флаги `enabled` / `ml_confidence` / `trained_risk` / `inverted`. Новые id из pack по умолчанию **OFF**. `legacy_disabled` всегда выкл.

**Деплой (`deploy_prod_ml.ps1`):** `enabled_strategies.json` / `bot_strategies.json` **не заливаются** на VPS. Перед apply — pull с VPS; на сервере `merge_enabled_strategies_from_pack.py` только дописывает новые id (OFF) + бэкап `enabled_strategies.bak.*.json`.

---

## 6. Текущий prod enable set (ориентир)

Включены **top-39** из pack (`prod_top30_pack.json`): Alt volume breakout + top-30 + **8 Jul legacy** (april-cut retrain, sigmoid) в конце списка (#32–39).
`CriptoPairsStrategy` — единственный в `legacy_disabled` (выкл).

UI: `#num` стабильный; сверху по `ui_order=0` — **`AltVolumeBreakoutStrategy`** (`scalp_liq_breakout`, **num=31**, ML PnL ~135, exp `exp31_mlp`, gate 0.55).

Далее по ML PnL (num 1…30), затем legacy хвост:
- #32 `AdxMomentumStrategy` (`trend_breakout`) — ML PnL 928 · gate 70%
- #33 `BollingerRsiStrategy` (`lite_mean_rev`) — 514 · 70%
- #34 `MacdEmaStrategy` (`trend_macd_ema`) — 450 · 45%
- #35 `SupertrendStrategy` (`trend_supertrend`) — 157 · 70%
- #36 `TripleEmaStrategy` (`trend_ema`) — 152 · 55%
- #37 `LiteRangeStrategy` (`lite_range`) — 88 · 65%
- #38 `LiteIntradayStrategy` (`lite_intraday`) — 19 · 45%
- #39 `FibPullbackStrategy` (`trend_fib`) — 15 · 45%
- … полный top-31 см. `simulation/config/prod_top30_pack.json`

UI **«Тестовые стратегии»** (`test_group`, nums 101+; **первый** блок панели; отдельные `enter_tag` / scenario, не live #1/#2/#32/#35):
- #101 `PsaraFlipTestStrategy` (`new_psar_test`)
- #102 `AtrChannelBreakoutTestStrategy` (`chart3_atrch_test`)
- #103 `AdxMomentumTestStrategy` (`trend_breakout_test`)
- #104 `SupertrendTestStrategy` (`trend_supertrend_test`) — 1x · SL −3% · без chase · выход `st_flip`

Порядок панели: Тестовые → Стратегии → Grid → Bybit Grid → ML Finder (внизу, обычно disabled).

Тестовый блок имеет **свои** настройки (`user_data/test_strategy_settings.json`, без рестарта): max open / на одну / stake / fallback SL·TP. Роутер режет входы тестовых тегов по этим лимитам; stake через `custom_stake_amount`.

ML gate (strategy bots): `profit_only`, floor `min_confidence` **0.45**; per-scenario порог из `pnl_classifier_meta.json` (`min_profit_proba`).
PnL classifiers: **sigmoid** calibration (smooth confidence %); isotonic historically collapsed live scores to 0%/100%.
Per-strategy UI toggle **«SL/TP из обучения»** (`trained_risk` в `enabled_strategies.json`): ON → `TAG_RISK` / sim schedule; OFF → глобальные SL/TP из настроек.
Per-strategy UI **«Уверенность ML»** (`ml_confidence` в `enabled_strategies.json`): порог `profit_proba` 45–95%; default = `min_profit_proba` из pack/meta. Gate читает override без рестарта бота.
Архив прежних моделей: `archives/prod_models_*.zip`.
Legacy retrain отчёт: `simulation/results/legacy_april_cut_ml/report.json`.

## 7. Симуляции и ML

| Что | Где |
|-----|-----|
| Сценарии ботов | `simulation/config/player_scenarios.json` |
| Какие сценарии «прод-набор» | `simulation/config/prod_ml_bots.json` |
| Свечи | `simulation/data/ctengine/` (раньше `freqtrade/`) |
| Player UI | `http://127.0.0.1:18999` — `simulation/scripts/start_player.ps1` |
| Сравнение стратегий | `simulation/scripts/run_scalp_strategies_compare.py` |
| Отчёты | `simulation/results/scalp_compare/`, `newset_compare/` |
| Train PnL classifier | `simulation/scripts/train_pnl_classifier.py` |
| Sync в site | `apply_prod_ml_config.py` |

**Важно:** в player/backtest per-scenario SL/TP должны попадать в конфиг сессии (`patch_whitelist` / scenario keys). Иначе общий config SL перетирает class attrs стратегии.

Типичный ML split (исторический): train `20250101–20260430`, test `20260501–20260625`.

---

## 8. Именование и бренд

| Было | Стало |
|------|-------|
| freqtrade / freqtrade.exe | ctengine / ctbot |
| `FREQTRADE__*` | `CTENGINE__*` |
| unit `freqtrade` / старые имена | `cryptotools-strategy`, `cryptotools-grid`, … |

В UI/продукте — **CryptoTools / CriptoTools**. В коде пакета — `ctengine`.

---

## 9. Типичные задачи → куда идти

| Задача | Куда |
|--------|------|
| Включить/выключить стратегию на live | `enabled_strategies.json` (+ apply/deploy) |
| Поменять SL/TP top-4 | `TAG_RISK` в `MultiStrategyRouter` + scenarios |
| Лимит сделок на одну стратегию | UI «На одну стратегию» → `max_open_trades_per_strategy.json` (0 = без лимита); читает роутер без reload |
| Новая идея стратегии | сначала `simulation/`, потом wrapper → prod |
| Деплой на VPS | `site/scripts/deploy_prod_ml.ps1` |
| Логи strategy | `journalctl -u cryptotools-strategy -n 100` |
| Whitelist / stake / max trades | pair-config UI или `config_*.json` |
| ML порог входа | `prod_ml_bots.json` → apply → deploy |
| Иконка/UI | `site/custom-ui/` |

---

## 10. Чего агенту НЕ делать

- Не запускать live-торговлю локально без просьбы.
- Не `push --force`, не менять git config.
- Не писать exploit/атаки на биржу/VPS.
- Не «чинить» баланс/маржу костылями в стратегии без запроса (часто просто мало free USDT).
- Не переименовывать id стратегий в проде без миграции открытых сделок/`enter_tag`.
- Не полагаться на устаревший README в `site/` про «только 6 стратегий» — смотри `enabled_strategies.json` и этот файл.

---

## 11. Быстрые команды

```powershell
# Local strategy (осторожно — нужен .env)
cd D:\cryptotools\site
.\scripts\load_env.ps1
.\.venv\Scripts\ctbot.exe trade --config user_data\config_strategy.json --strategy MultiStrategyRouter

# Deploy prod
cd D:\cryptotools\site\scripts
.\deploy_prod_ml.ps1

# Sim player
cd D:\cryptotools\simulation\scripts
.\start_player.ps1 -SkipDownload
```

```bash
# VPS check
systemctl is-active cryptotools-strategy cryptotools-grid pair-config
journalctl -u cryptotools-strategy -n 80 --no-pager
```

---

## 12. Поддержка документа

При смене архитектуры (новые боты, другой risk model, другой деплой) — **обнови этот файл в том же PR/сессии**.  
Дата ориентира: **2026-08-09**.

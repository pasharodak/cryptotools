#!/usr/bin/env python3
"""Execute shared signal-engine entries per user Bybit keys."""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import sqlite3
import sys
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASE = Path(os.environ.get("CT_BASE", "/home/cryptotools/app"))
SCRIPTS = BASE / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(BASE / "user_data") not in sys.path:
    sys.path.insert(0, str(BASE / "user_data"))

import signal_bus  # noqa: E402
import tenant_manager as tm  # noqa: E402
import trade_exit_monitor  # noqa: E402
import user_trading  # noqa: E402
import pair_entry_guard  # noqa: E402
from reconcile_positions import archive_trade_in_db  # noqa: E402
from user_exchange import UserBybitExchange, fetch_public_klines, ft_pair_to_symbol  # noqa: E402

try:
    from ml.gate import allow_trade_entry, get_live_ml_gate  # noqa: E402
except ImportError:
    allow_trade_entry = None  # type: ignore
    get_live_ml_gate = None  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Windows redirected stdout/stderr often uses a legacy codepage; force UTF-8 for Cyrillic logs.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
# Dedicated UTF-8 file (Start-Process redirects often corrupt Cyrillic).
try:
    _log_dir = BASE / "user_data" / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _fh = RotatingFileHandler(
        _log_dir / "trade-executor.log", maxBytes=50 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _fh.setFormatter(
        logging.Formatter("%(asctime)s UTC - %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logging.getLogger().addHandler(_fh)
except Exception:
    pass
log = logging.getLogger("trade_executor")

_running = True
_processed: set[str] = set()
_exchanges: dict[str, UserBybitExchange] = {}
_ohlcv_cache: dict[str, tuple[float, Any]] = {}
_pair_entry_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
# (monotonic_ts, category) — for STATUS “why no trades” summary in UI logs
_recent_skip_cats: deque[tuple[float, str]] = deque(maxlen=400)
_recent_entry_ok = 0
OHLCV_CACHE_TTL = 60.0
MAX_EVENTS_PER_LOOP = 80
EXIT_INTERVAL_SEC = float(os.environ.get("CT_EXIT_INTERVAL_SEC", "30"))
HEARTBEAT_PATH = BASE / "user_data" / "logs" / "trade-executor.heartbeat"


def _skip_category(reason: str) -> str:
    r = (reason or "").lower()
    if "ml gate" in r:
        return "ML gate"
    if "whitelist" in r:
        return "whitelist"
    if "выключена" in r or "enabled" in r:
        return "strategy off"
    if "лимит" in r or "max_open" in r:
        return "лимит сделок"
    if "уже есть" in r or "позици" in r:
        return "уже в позиции"
    if "api" in r or "ключ" in r or "readOnly" in r or "permission" in r:
        return "API/ключ"
    if "stoploss" in r or "order fail" in r:
        return "биржа/ордер"
    return "другое"


def _record_skip(reason: str) -> None:
    _recent_skip_cats.append((time.monotonic(), _skip_category(reason)))


def _skip_summary(window_sec: float = 300.0) -> str:
    now = time.monotonic()
    counts = Counter(cat for ts, cat in _recent_skip_cats if now - ts <= window_sec)
    if not counts:
        return "за 5м отказов нет (мало сигналов или все прошли gate)"
    parts = [f"{cat}×{n}" for cat, n in counts.most_common(6)]
    return "за 5м отказы: " + ", ".join(parts)


def _touch_heartbeat() -> None:
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _stop(*_args: object) -> None:
    global _running
    _running = False


if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


def tenant_dir(user_id: str) -> Path:
    if user_id == "admin":
        return BASE / "user_data"
    return tm.tenant_user_data(user_id)


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_user_config(user_id: str) -> dict[str, Any]:
    return load_json(tenant_dir(user_id) / "config_strategy.json", {})


def _strategy_stake(user_id: str, tag: str, cfg: dict[str, Any]) -> float:
    """1h entries use the user's 1h stake. Everything else uses the strategy stake."""
    base = trade_exit_monitor.base_enter_tag(tag)
    try:
        from htf1h_catalog import HTF1H_IDS
    except Exception:
        HTF1H_IDS = frozenset()
    if base in HTF1H_IDS:
        try:
            import user_prefs as up

            blob = up.load_bot_limits_payload(user_id)
            raw = blob.get("htf_stake_amount") if isinstance(blob, dict) else None
            if raw is not None:
                return float(raw)
        except Exception:
            pass
    try:
        return float(cfg.get("stake_amount") or 5)
    except (TypeError, ValueError):
        return 5.0


def load_enabled(user_id: str) -> dict[str, bool]:
    try:
        import user_prefs as up

        payload = up.load_enabled_payload(user_id)
        enabled = payload.get("enabled") if isinstance(payload, dict) else {}
        if not isinstance(enabled, dict):
            return {}
        return {str(k): bool(v) for k, v in enabled.items()}
    except Exception:
        data = load_json(tenant_dir(user_id) / "enabled_strategies.json", {})
        enabled = data.get("enabled") if isinstance(data, dict) else {}
        if not isinstance(enabled, dict):
            return {}
        return {str(k): bool(v) for k, v in enabled.items()}


def _strategy_base_id(sid: str) -> str:
    raw = str(sid or "")
    while True:
        low = raw.lower()
        if low.endswith(":hedge"):
            raw = raw[: -len(":hedge")]
        elif low.endswith(":inv"):
            raw = raw[: -len(":inv")]
        else:
            return raw


def _normalize_signal_side(side: str) -> str:
    raw = str(side or "long").strip().lower()
    if raw in {"short", "sell"}:
        return "short"
    return "long"


def _normalize_allowed_side(value: Any) -> str:
    raw = str(value or "both").strip().lower()
    if raw in {"long", "buy"}:
        return "long"
    if raw in {"short", "sell"}:
        return "short"
    return "both"


def entry_side_skip(allowed: str, signal_side: str, strategy_id: str) -> str | None:
    """None when the published side is allowed. Filter is after invert."""
    want = _normalize_allowed_side(allowed)
    got = _normalize_signal_side(signal_side)
    if want in {"long", "short"} and got != want:
        label = "лонг" if want == "long" else "шорт"
        got_label = "шорт" if got == "short" else "лонг"
        name = strategy_id or "стратегия"
        return f"{name}: направление {got_label} выключено (только {label})"
    return None


def load_entry_sides(user_id: str) -> dict[str, str]:
    try:
        import user_prefs as up

        data = up.load_enabled_payload(user_id)
    except Exception:
        data = load_json(tenant_dir(user_id) / "enabled_strategies.json", {})
    raw = data.get("entry_sides") if isinstance(data, dict) else {}
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            side = _normalize_allowed_side(value)
            if side != "both":
                out[str(key)] = side
    return out


def load_ml_confidence(user_id: str) -> dict[str, float]:
    try:
        import user_prefs as up

        data = up.load_enabled_payload(user_id)
    except Exception:
        data = load_json(tenant_dir(user_id) / "enabled_strategies.json", {})
    raw = data.get("ml_confidence") if isinstance(data, dict) else {}
    out: dict[str, float] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
    return out


def load_test_settings(user_id: str) -> dict[str, Any]:
    try:
        import user_prefs as up

        return up.load_test_settings(user_id)
    except Exception:
        return load_json(tenant_dir(user_id) / "test_strategy_settings.json", {})


def load_test_strategy_ids(user_id: str) -> set[str]:
    """Test-panel strategy ids from per-user placement (not in main, not hidden)."""
    try:
        import user_prefs as up

        placement = up.load_placement(user_id)
        main = {str(x) for x in (placement.get("main") or [])}
        hidden = {str(x) for x in (placement.get("hidden") or [])}
        # Prefer enabled map keys + known registry via enabled payload / fallback
        payload = up.load_enabled_payload(user_id)
        catalog = set((payload.get("enabled") or {}).keys())
        if not catalog:
            catalog = set(load_enabled(user_id).keys())
        # Also include any class currently known from open trades / defaults
        for sid in list(catalog):
            pass
        # Build from all strategies that appear in enabled payload + placement-aware defaults
        from pathlib import Path

        strat_dir = Path(__file__).resolve().parent.parent / "user_data" / "strategies"
        if strat_dir.is_dir():
            for p in strat_dir.glob("*Strategy.py"):
                catalog.add(p.stem)
        return {sid for sid in catalog if sid not in main and sid not in hidden}
    except Exception:
        path = tenant_dir(user_id) / "strategy_ui_placement.json"
        data = load_json(path, {})
        main = {str(x) for x in (data.get("main") or [])} if isinstance(data, dict) else set()
        hidden = {str(x) for x in (data.get("hidden") or [])} if isinstance(data, dict) else set()
        fallback = set(load_enabled(user_id).keys())
        return {sid for sid in fallback if sid not in main and sid not in hidden}


def load_limits(user_id: str) -> dict[str, int]:
    """Slot caps from SQLite user_settings.bot_limits (never live show_config).

    Main and test pools are separate: test_max must NOT inflate the main
    strategy ceiling (that previously made UI/executor show open=N/100 while
    bot_limits.strategy was 1).
    """
    max_open = 0
    per = 0
    try:
        import user_prefs as up

        bl = up.load_bot_limits_payload(user_id)
        trades = bl.get("max_open_trades") if isinstance(bl, dict) else {}
        if isinstance(trades, dict) and trades.get("strategy") is not None:
            max_open = int(trades["strategy"])
        per_blob = up.load_max_per_strategy(user_id)
        per = max(0, int(per_blob.get("value") or 0))
        if isinstance(bl, dict) and bl.get("max_open_trades_per_strategy") is not None:
            try:
                per = max(0, int(bl["max_open_trades_per_strategy"]))
            except (TypeError, ValueError):
                pass
    except Exception:
        cfg = load_user_config(user_id)
        max_open = int(cfg.get("max_open_trades") or 0)
        per_path = tenant_dir(user_id) / "max_open_trades_per_strategy.json"
        per = 0
        data = load_json(per_path, {})
        if isinstance(data, dict):
            try:
                per = max(0, int(data.get("value") or 0))
            except (TypeError, ValueError):
                per = 0
    test = load_test_settings(user_id)
    try:
        test_max = max(0, int(test.get("max_open_trades") or 0))
    except (TypeError, ValueError):
        test_max = 0
    try:
        test_per = max(0, int(test.get("max_open_trades_per_strategy") or 0))
    except (TypeError, ValueError):
        test_per = 0
    return {
        "max_open_trades": max_open,
        "max_per_strategy": per,
        "test_max_open_trades": test_max,
        "test_max_per_strategy": test_per,
    }


# AdxMomentum shorts underperform live — allow long only (main + test tags).
ADX_LONG_ONLY_TAGS = frozenset({"AdxMomentumStrategy", "AdxMomentumTestStrategy"})


def db_path(user_id: str) -> Path:
    cfg = load_user_config(user_id)
    raw = str(cfg.get("db_url") or "sqlite:///tradesv3-strategy.sqlite")
    rel = raw.replace("sqlite:///", "").lstrip("/")
    path = Path(rel)
    if path.is_absolute():
        return path
    # Prefer user_data/<name> for bare filenames (local Windows cwd quirks).
    name = Path(rel).name
    ud = tenant_dir(user_id) / name
    legacy = BASE / rel
    if ud.is_file():
        return ud
    if legacy.is_file():
        return legacy
    return ud


def ensure_trades_db(user_id: str) -> Path:
    """Create admin/tenant trades DB with ctengine-compatible schema if missing."""
    path = db_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.is_file()
    schema_trades = """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER NOT NULL PRIMARY KEY,
        exchange VARCHAR(25) NOT NULL,
        pair VARCHAR(25) NOT NULL,
        base_currency VARCHAR(25),
        stake_currency VARCHAR(25),
        is_open BOOLEAN NOT NULL,
        fee_open FLOAT NOT NULL,
        fee_open_cost FLOAT,
        fee_open_currency VARCHAR(25),
        fee_close FLOAT NOT NULL,
        fee_close_cost FLOAT,
        fee_close_currency VARCHAR(25),
        open_rate FLOAT NOT NULL,
        open_rate_requested FLOAT,
        open_trade_value FLOAT,
        close_rate FLOAT,
        close_rate_requested FLOAT,
        realized_profit FLOAT,
        close_profit FLOAT,
        close_profit_abs FLOAT,
        stake_amount FLOAT NOT NULL,
        max_stake_amount FLOAT,
        amount FLOAT NOT NULL,
        amount_requested FLOAT,
        open_date DATETIME NOT NULL,
        close_date DATETIME,
        stop_loss FLOAT,
        stop_loss_pct FLOAT,
        initial_stop_loss FLOAT,
        initial_stop_loss_pct FLOAT,
        is_stop_loss_trailing BOOLEAN NOT NULL,
        max_rate FLOAT,
        min_rate FLOAT,
        exit_reason VARCHAR(255),
        exit_order_status VARCHAR(100),
        strategy VARCHAR(100),
        enter_tag VARCHAR(255),
        timeframe INTEGER,
        trading_mode VARCHAR(7),
        amount_precision FLOAT,
        price_precision FLOAT,
        precision_mode INTEGER,
        precision_mode_price INTEGER,
        contract_size FLOAT,
        leverage FLOAT,
        is_short BOOLEAN NOT NULL,
        liquidation_price FLOAT,
        interest_rate FLOAT NOT NULL,
        funding_fees FLOAT,
        funding_fee_running FLOAT,
        record_version INTEGER NOT NULL
    )
    """
    schema_custom = """
    CREATE TABLE IF NOT EXISTS trade_custom_data (
        id INTEGER NOT NULL PRIMARY KEY,
        ft_trade_id INTEGER NOT NULL,
        cd_key VARCHAR(255) NOT NULL,
        cd_type VARCHAR(25) NOT NULL,
        cd_value TEXT NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME,
        UNIQUE (ft_trade_id, cd_key)
    )
    """
    schema_orders = """
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER NOT NULL PRIMARY KEY,
        ft_trade_id INTEGER NOT NULL,
        ft_order_side VARCHAR(25) NOT NULL,
        ft_pair VARCHAR(25) NOT NULL,
        ft_is_open BOOLEAN NOT NULL DEFAULT 1,
        ft_amount FLOAT NOT NULL,
        ft_price FLOAT NOT NULL,
        ft_cancel_reason VARCHAR(255),
        order_id VARCHAR(255) NOT NULL,
        status VARCHAR(255),
        symbol VARCHAR(25),
        order_type VARCHAR(50),
        side VARCHAR(25),
        price FLOAT,
        average FLOAT,
        amount FLOAT,
        filled FLOAT,
        remaining FLOAT,
        cost FLOAT,
        stop_price FLOAT,
        order_date DATETIME,
        order_filled_date DATETIME,
        order_update_date DATETIME,
        funding_fee FLOAT,
        ft_fee_base FLOAT,
        ft_order_tag VARCHAR(255)
    )
    """
    with sqlite3.connect(path) as conn:
        conn.execute(schema_trades)
        conn.execute(schema_custom)
        conn.execute(schema_orders)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_trade_custom_data_ft_trade_id "
            "ON trade_custom_data (ft_trade_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_orders_ft_trade_id ON orders (ft_trade_id)"
        )
        conn.commit()
    if created:
        log.info("created trades DB %s", path)
    return path


def get_exchange(user_id: str) -> UserBybitExchange | None:
    if user_id in _exchanges:
        return _exchanges[user_id]
    key = ""
    secret = ""
    demo_trading = False
    if user_id == "admin":
        blob = tm._load_secrets_blob("admin")
        key = str(blob.get("bybit_api_key") or "").strip()
        secret = str(blob.get("bybit_api_secret") or "").strip()
        demo_trading = bool(tm._as_bool(blob.get("bybit_demo_trading")))
        if not key or not secret:
            key, secret, demo_trading = tm._admin_env_credentials()
    else:
        sec = tm.load_user_secrets(user_id)
        if sec:
            key = str(sec.get("bybit_api_key") or "").strip()
            secret = str(sec.get("bybit_api_secret") or "").strip()
            demo_trading = str(sec.get("bybit_demo_trading") or "").strip().lower() in (
                "1",
                "true",
                "t",
                "yes",
                "y",
                "on",
            ) or sec.get("bybit_demo_trading") is True
        if not key or not secret:
            env_path = tenant_dir(user_id) / ".env"
            if env_path.is_file():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k == "BYBIT_API_KEY" and not key:
                        key = v
                    elif k == "BYBIT_API_SECRET" and not secret:
                        secret = v
                    elif k in ("BYBIT_DEMO_TRADING", "CTENGINE__EXCHANGE__DEMO_TRADING"):
                        demo_trading = v.lower() in ("1", "true", "t", "yes", "y", "on")
    if not key or not secret:
        return None
    try:
        ex = UserBybitExchange(key, secret, demo_trading=demo_trading)
        try:
            skew = ex.sync_time()
            log.info(
                "bybit time sync user=%s demo=%s skew_ms=%s base=%s",
                user_id,
                demo_trading,
                skew,
                ex.api_base,
            )
        except Exception as exc:
            log.warning("bybit time sync failed user=%s: %s", user_id, exc)
    except ValueError:
        return None
    _exchanges[user_id] = ex
    return ex


def count_open_by_panel(user_id: str, *, test: bool) -> int:
    """Count open trades in main vs test panel (by enter_tag / strategy id)."""
    test_ids = load_test_strategy_ids(user_id)
    path = db_path(user_id)
    if not path.is_file():
        return 0
    try:
        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                "SELECT enter_tag, strategy FROM trades WHERE is_open=1"
            ).fetchall()
    except sqlite3.Error:
        return 0
    n = 0
    for enter_tag, strategy in rows:
        tag = trade_exit_monitor.base_enter_tag(str(enter_tag or strategy or ""))
        is_test = tag in test_ids
        if is_test == test:
            n += 1
    return n


def _llm_open_count(user_id: str) -> int:
    path = db_path(user_id)
    if not path.is_file():
        return 0
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM trades
                WHERE is_open=1 AND (
                    enter_tag LIKE 'DeepseekLlmStrategy%'
                    OR strategy='DeepseekLlmStrategy'
                )
                """
            ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] if row else 0)


def _llm_skip_reason(user_id: str, ev: dict[str, Any]) -> str | None:
    import llm_settings

    pair = str(ev.get("pair") or "")
    side = str(ev.get("side") or "long")
    allowed = set(llm_settings.pairs_for(user_id))
    if pair not in allowed:
        return f"DeepSeek не торгует {pair or '?'}"
    settings = llm_settings.load(user_id)
    conf = llm_settings.signal_confidence(ev)
    need = float(settings["min_confidence"])
    if conf is None:
        return "DeepSeek: в сигнале нет уверенности"
    if conf < need:
        return f"DeepSeek уверенность {conf * 100:.0f}% ниже порога {need * 100:.0f}%"
    cap = int(settings["max_open_trades"])
    if cap <= 0:
        return "DeepSeek: макс. сделок = 0"
    open_n = _llm_open_count(user_id)
    if open_n >= cap:
        return f"DeepSeek лимит открытых сделок {open_n}/{cap}"
    if count_open_on_pair(user_id, pair) > 0:
        return f"уже есть открытая сделка по {pair} в БД"
    try:
        if exchange_has_position(user_id, pair, side):
            return f"на бирже уже есть позиция {side} по {pair}"
    except Exception as exc:
        return f"не удалось проверить позиции на бирже: {exc}"
    busy = pair_entry_guard.busy_reason(
        pair,
        side,
        bot="llm",
        tenant_id=user_id,
        check_bybit=(user_id == "admin"),
    )
    if busy:
        return busy
    return None


def _llm50_skip_reason(user_id: str, ev: dict[str, Any]) -> str | None:
    import llm_book

    pair = str(ev.get("pair") or "")
    side = str(ev.get("side") or "long")
    if pair not in set(llm_book.pairs()):
        return f"DeepSeek 50 не торгует {pair or '?'}"
    positions = llm_book.open_positions(llm_book.trades_db_for(user_id))
    if any(row["pair"] == pair for row in positions):
        return f"DeepSeek 50 уже держит {pair}"
    try:
        stake = float(ev.get("stake_usdt") or 0)
    except (TypeError, ValueError):
        stake = 0.0
    free = llm_book.free_margin(positions)
    if stake < llm_book.MIN_STAKE or stake > free + 1e-6:
        return f"DeepSeek 50 бюджет: нужно {stake:.2f}, свободно {free:.2f} из {llm_book.BUDGET_USDT:.0f}"
    if count_open_on_pair(user_id, pair) > 0:
        return f"уже есть открытая сделка по {pair} в БД"
    try:
        if exchange_has_position(user_id, pair, side):
            return f"на бирже уже есть позиция {side} по {pair}"
    except Exception as exc:
        return f"не удалось проверить позиции на бирже: {exc}"
    busy = pair_entry_guard.busy_reason(
        pair,
        side,
        bot="llm50",
        tenant_id=user_id,
        check_bybit=(user_id == "admin"),
    )
    if busy:
        return busy
    return None


def _cursor_open_count(user_id: str) -> int:
    path = db_path(user_id)
    if not path.is_file():
        return 0
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM trades
                WHERE is_open=1 AND (
                    enter_tag LIKE 'CursorLlmStrategy%'
                    OR strategy='CursorLlmStrategy'
                )
                """
            ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] if row else 0)


def _cursor_skip_reason(user_id: str, ev: dict[str, Any]) -> str | None:
    import cursor_settings

    pair = str(ev.get("pair") or "")
    side = str(ev.get("side") or "long")
    allowed = set(cursor_settings.pairs_for(user_id))
    if pair not in allowed:
        return f"Cursor не торгует {pair or '?'}"
    settings = cursor_settings.load(user_id)
    conf = cursor_settings.signal_confidence(ev)
    need = float(settings["min_confidence"])
    if conf is None:
        return "Cursor: в сигнале нет уверенности"
    if conf < need:
        return f"Cursor уверенность {conf * 100:.0f}% ниже порога {need * 100:.0f}%"
    cap = int(settings["max_open_trades"])
    if cap <= 0:
        return "Cursor: макс. сделок = 0"
    open_n = _cursor_open_count(user_id)
    if open_n >= cap:
        return f"Cursor лимит открытых сделок {open_n}/{cap}"
    if count_open_on_pair(user_id, pair) > 0:
        return f"уже есть открытая сделка по {pair} в БД"
    try:
        if exchange_has_position(user_id, pair, side):
            return f"на бирже уже есть позиция {side} по {pair}"
    except Exception as exc:
        return f"не удалось проверить позиции на бирже: {exc}"
    busy = pair_entry_guard.busy_reason(
        pair,
        side,
        bot="cursor",
        tenant_id=user_id,
        check_bybit=(user_id == "admin"),
    )
    if busy:
        return busy
    return None


def _cursor50_skip_reason(user_id: str, ev: dict[str, Any]) -> str | None:
    import cursor_book

    pair = str(ev.get("pair") or "")
    side = str(ev.get("side") or "long")
    if pair not in set(cursor_book.pairs()):
        return f"Cursor 50 не торгует {pair or '?'}"
    positions = cursor_book.open_positions(cursor_book.trades_db_for(user_id))
    if any(row["pair"] == pair for row in positions):
        return f"Cursor 50 уже держит {pair}"
    try:
        stake = float(ev.get("stake_usdt") or 0)
    except (TypeError, ValueError):
        stake = 0.0
    free = cursor_book.free_margin(positions)
    if stake < cursor_book.MIN_STAKE or stake > free + 1e-6:
        return f"Cursor 50 бюджет: нужно {stake:.2f}, свободно {free:.2f} из {cursor_book.BUDGET_USDT:.0f}"
    if count_open_on_pair(user_id, pair) > 0:
        return f"уже есть открытая сделка по {pair} в БД"
    try:
        if exchange_has_position(user_id, pair, side):
            return f"на бирже уже есть позиция {side} по {pair}"
    except Exception as exc:
        return f"не удалось проверить позиции на бирже: {exc}"
    busy = pair_entry_guard.busy_reason(
        pair,
        side,
        bot="cursor50",
        tenant_id=user_id,
        check_bybit=(user_id == "admin"),
    )
    if busy:
        return busy
    return None


def _price_action_open_count(user_id: str) -> int:
    path = db_path(user_id)
    if not path.is_file():
        return 0
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM trades
                WHERE is_open=1 AND (
                    enter_tag LIKE 'PaTrendPullback%'
                    OR enter_tag LIKE 'PaLiquiditySweep%'
                    OR strategy IN ('PaTrendPullback', 'PaLiquiditySweep')
                )
                """
            ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] if row else 0)


def _price_action_skip_reason(user_id: str, ev: dict[str, Any]) -> str | None:
    import price_action_settings

    pair = str(ev.get("pair") or "")
    side = str(ev.get("side") or "long")
    settings = price_action_settings.load(user_id)
    if pair not in set(settings["pairs"]):
        return f"Price Action не торгует {pair or '?'}"
    raw_score = ev.get("score")
    if raw_score is None:
        ind = ev.get("indicators") if isinstance(ev.get("indicators"), dict) else {}
        raw_score = str(ind.get("pa_score") or "").split("/", 1)[0]
    try:
        score = int(float(raw_score))
    except (TypeError, ValueError):
        return "Price Action: в сигнале нет скора"
    if score < int(settings["min_score"]):
        return f"Price Action скор {score}/8 ниже порога {settings['min_score']}/8"
    try:
        rr_net = float(ev.get("rr_net"))
    except (TypeError, ValueError):
        return "Price Action: в сигнале нет R/R"
    if rr_net < float(settings["min_rr"]):
        return f"Price Action R/R {rr_net:.2f} ниже порога {settings['min_rr']:.2f}"
    try:
        spread = float(ev.get("spread_pct"))
    except (TypeError, ValueError):
        return "Price Action: в сигнале нет спреда"
    if spread > float(settings["max_spread_pct"]):
        return f"Price Action спред {spread:.3f}% выше {settings['max_spread_pct']:.3f}%"
    try:
        depth = float(ev.get("depth_usdt"))
    except (TypeError, ValueError):
        depth = 0.0
    if depth < float(settings["min_depth_usdt"]):
        return (
            f"Price Action глубина ${depth:,.0f} ниже "
            f"${float(settings['min_depth_usdt']):,.0f}"
        )
    try:
        risk_pct = float(ev.get("risk_pct"))
    except (TypeError, ValueError):
        risk_pct = 999.0
    if risk_pct > float(settings["max_risk_pct"]):
        return f"Price Action риск {risk_pct:.2f}% выше {settings['max_risk_pct']:.2f}%"
    try:
        entry = float(ev.get("rate") or ev.get("entry") or 0)
        sl_price = float(ev.get("sl_price") or 0)
        tp_price = float(ev.get("tp1_price") or 0)
    except (TypeError, ValueError):
        return "Price Action: нет цен входа/стопа/тейка"
    if entry <= 0 or sl_price <= 0 or tp_price <= 0:
        return "Price Action: нет цен входа/стопа/тейка"
    if price_action_settings.trade_ratios(
        entry, sl_price, tp_price, float(settings["leverage"])
    ) is None:
        return "Price Action: некорректные стоп/тейк"
    cap = int(settings["max_open_trades"])
    if cap <= 0:
        return "Price Action: макс. сделок = 0"
    open_n = _price_action_open_count(user_id)
    if open_n >= cap:
        return f"Price Action лимит открытых сделок {open_n}/{cap}"
    if count_open_on_pair(user_id, pair) > 0:
        return f"уже есть открытая сделка по {pair} в БД"
    try:
        if exchange_has_position(user_id, pair, side):
            return f"на бирже уже есть позиция {side} по {pair}"
    except Exception as exc:
        return f"не удалось проверить позиции на бирже: {exc}"
    busy = pair_entry_guard.busy_reason(
        pair,
        side,
        bot="price_action",
        tenant_id=user_id,
        check_bybit=(user_id == "admin"),
    )
    if busy:
        return busy
    return None


def skip_reason(user_id: str, ev: dict[str, Any]) -> str | None:
    """Human-readable why this signal will not open a trade (None = allowed)."""
    sid = str(ev.get("strategy_id") or "")
    pair = str(ev.get("pair") or "")
    side = str(ev.get("side") or "long")
    bot = str(ev.get("bot") or "")
    if bot in user_trading.RETIRED_BOTS:
        return f"{bot} выведен из торговли"
    if bot == "price_action":
        return _price_action_skip_reason(user_id, ev)
    if bot == "llm":
        return _llm_skip_reason(user_id, ev)
    if str(ev.get("bot") or "") == "llm50":
        return _llm50_skip_reason(user_id, ev)
    if str(ev.get("bot") or "") == "cursor":
        return _cursor_skip_reason(user_id, ev)
    if str(ev.get("bot") or "") == "cursor50":
        return _cursor50_skip_reason(user_id, ev)
    if not sid:
        return "нет strategy_id в сигнале"
    enabled = load_enabled(user_id)
    if not enabled:
        return f"пустой enabled_strategies для {user_id}"
    if not enabled.get(sid, False):
        return f"стратегия {sid} выключена у пользователя"
    base_sid = _strategy_base_id(sid)
    allowed_side = load_entry_sides(user_id).get(base_sid, "both")
    blocked_side = entry_side_skip(allowed_side, side, base_sid or sid)
    if blocked_side:
        return blocked_side
    if sid in ADX_LONG_ONLY_TAGS and side.lower() in ("short", "sell"):
        return f"{sid}: shorts отключены (только long)"
    if not pair:
        return "нет pair в сигнале"
    blocked = pair_block_reason(user_id, sid, pair)
    if blocked:
        return blocked
    if count_open_on_pair(user_id, pair) > 0:
        return f"уже есть открытая сделка по {pair} в БД"
    try:
        if exchange_has_position(user_id, pair, side):
            return f"на бирже уже есть позиция {side} по {pair}"
    except Exception as exc:
        return f"не удалось проверить позиции на бирже: {exc}"
    # Cross-bot first-wins (Finder/Grid/Strategy share one Bybit account for admin).
    busy = pair_entry_guard.busy_reason(
        pair,
        side,
        bot="strategy",
        tenant_id=user_id,
        check_bybit=(user_id == "admin"),
    )
    if busy:
        return busy
    limits = load_limits(user_id)
    test_ids = load_test_strategy_ids(user_id)
    if sid in test_ids:
        test_cap = int(limits.get("test_max_open_trades") or 0)
        if test_cap <= 0:
            return "max_open_trades=0 для тестовых (лимит выключен/ноль)"
        test_open = count_open_by_panel(user_id, test=True)
        if test_open >= test_cap:
            return f"лимит тестовых стратегий {test_open}/{test_cap}"
        per = int(limits.get("test_max_per_strategy") or 0)
    else:
        main_cap = int(limits.get("max_open_trades") or 0)
        if main_cap <= 0:
            return "max_open_trades=0 (лимит сделок выключен/ноль)"
        main_open = count_open_by_panel(user_id, test=False)
        if main_open >= main_cap:
            return f"лимит открытых сделок {main_open}/{main_cap}"
        per = limits["max_per_strategy"]
    if per > 0:
        tag_n = count_open_trades(user_id, tag=sid)
        if tag_n >= per:
            return f"лимит на стратегию {sid}: {tag_n}/{per}"
    if not ml_allows(user_id, ev):
        raw = ev.get("ml") if isinstance(ev.get("ml"), dict) else {}
        min_conf = _ml_min_conf(
            user_id, sid, ev.get("scenario") if isinstance(ev.get("scenario"), dict) else None
        )
        return (
            f"ML gate отклонил: pred={raw.get('predicted') or '?'} "
            f"profit_conf={float(raw.get('confidence_profit') or 0)*100:.1f}% "
            f"min={min_conf*100:.0f}%"
        )
    return None


def should_enter(user_id: str, ev: dict[str, Any]) -> bool:
    return skip_reason(user_id, ev) is None


def execute_for_user(user_id: str, ev: dict[str, Any]) -> None:
    pair = str(ev.get("pair") or "")
    side = str(ev.get("side") or "long")
    lock_key = f"{user_id}:{pair}:{side.lower()}"
    with _pair_entry_locks[lock_key]:
        reason = skip_reason(user_id, ev)
        if reason is not None:
            _record_skip(reason)
            log.info(
                "SKIP user=%s %s %s tag=%s — %s",
                user_id,
                side,
                pair or "?",
                ev.get("strategy_id") or "?",
                reason,
            )
            return
        ex = get_exchange(user_id)
        if ex is None:
            _record_skip("нет API-ключей Bybit")
            log.warning(
                "SKIP user=%s %s %s — нет API-ключей Bybit (Настройки → Секреты)",
                user_id,
                side,
                pair,
            )
            return
        block = ex.trade_block_reason()
        if block:
            _record_skip(block)
            log.error(
                "SKIP user=%s %s %s tag=%s — %s",
                user_id,
                side,
                pair,
                ev.get("strategy_id") or "?",
                block,
            )
            return
        bot_name = str(ev.get("bot") or "")
        if bot_name == "llm":
            claim_bot = "llm"
        elif bot_name == "llm50":
            claim_bot = "llm50"
        elif bot_name == "cursor":
            claim_bot = "cursor"
        elif bot_name == "cursor50":
            claim_bot = "cursor50"
        elif bot_name == "price_action":
            claim_bot = "price_action"
        else:
            claim_bot = "strategy"
        ok_claim, claim_reason = pair_entry_guard.claim_pair(
            claim_bot,
            pair,
            side,
            tenant_id=user_id,
            check_bybit=(user_id == "admin"),
        )
        if not ok_claim:
            _record_skip(claim_reason or "pair busy")
            log.info(
                "SKIP user=%s %s %s tag=%s — %s",
                user_id,
                side,
                pair,
                ev.get("strategy_id") or "?",
                claim_reason,
            )
            return
        cfg = load_user_config(user_id)
        tag = str(ev.get("entry_tag") or ev.get("strategy_id") or "")
        leverage = 3.0
        if bot_name == "llm":
            import llm_settings

            stake = float(llm_settings.load(user_id)["stake_amount"])
        elif bot_name == "llm50":
            stake = float(ev.get("stake_usdt") or 0)
        elif bot_name == "cursor":
            import cursor_settings

            stake = float(cursor_settings.load(user_id)["stake_amount"])
        elif bot_name == "cursor50":
            stake = float(ev.get("stake_usdt") or 0)
        elif bot_name == "price_action":
            import price_action_settings

            pa = price_action_settings.load(user_id)
            stake = float(pa["stake_amount"])
            leverage = float(pa["leverage"])
        else:
            stake = _strategy_stake(user_id, tag, cfg)
        if bot_name in {"llm50", "cursor50"}:
            sl = float(ev.get("stop_loss_ratio"))
            tp = float(ev.get("take_profit_ratio"))
        elif bot_name == "price_action":
            ratios = price_action_settings.trade_ratios(
                float(ev.get("rate") or 0),
                float(ev.get("sl_price") or 0),
                float(ev.get("tp1_price") or 0),
                leverage,
            )
            if ratios is None:
                pair_entry_guard.release_pair(claim_bot, pair, side, tenant_id=user_id)
                _record_skip("Price Action: некорректные стоп/тейк")
                return
            sl, tp = ratios
        else:
            sl, tp = trade_exit_monitor.risk_thresholds(
                user_id,
                trade_exit_monitor.base_enter_tag(tag),
                tenant_dir=tenant_dir,
                load_json=load_json,
                load_user_config=load_user_config,
            )
        try:
            ensure_trades_db(user_id)
            res = ex.market_entry(
                pair=pair,
                side=side,
                stake_usdt=stake,
                leverage=leverage,
                stop_loss=sl,
                take_profit=tp,
            )
        except Exception as exc:
            pair_entry_guard.release_pair(claim_bot, pair, side, tenant_id=user_id)
            log.error(
                "ORDER FAIL user=%s %s %s tag=%s stake=%.2f — %s",
                user_id,
                side,
                pair,
                tag,
                stake,
                exc,
            )
            try:
                import telegram_links as tg_links

                tg_links.notify_user(
                    user_id,
                    tg_links.format_order_fail_ru(
                        pair=pair,
                        side=side,
                        strategy=tag,
                        error=str(exc),
                    ),
                    kind="other",
                )
            except Exception:
                pass
            return
        price = float(res.get("price") or ev.get("rate") or 0)
        qty = float(res.get("qty") or 0)
        try:
            insert_trade_row(
                user_id,
                pair=pair,
                strategy_id=str(ev.get("strategy_id") or tag),
                entry_tag=tag,
                is_short=side.lower() in ("short", "sell"),
                open_rate=price,
                stake_amount=stake,
                amount=qty,
                leverage=leverage,
                stop_loss_ratio=sl,
                take_profit_ratio=tp,
                ml_meta=_ml_meta_from_event(user_id, ev),
            )
        except Exception as exc:
            log.error(
                "DB FAIL after order user=%s pair=%s: %s — reconciling from exchange",
                user_id,
                pair,
                exc,
            )
            _reconcile_user_positions(user_id)
            return
        global _recent_entry_ok
        _recent_entry_ok += 1
        log.info(
            "ENTRY OK user=%s %s %s tag=%s stake=%.2f price=%.6f qty=%s order=%s demo=%s",
            user_id,
            side,
            pair,
            tag,
            stake,
            price,
            qty,
            res.get("order_id"),
            getattr(ex, "demo_trading", False),
        )
        try:
            import telegram_links as tg_links

            tg_links.notify_user(
                user_id,
                tg_links.format_entry_ru(
                    pair=pair,
                    side=side,
                    strategy=str(ev.get("strategy_id") or tag),
                    price=price,
                    stake=stake,
                    qty=qty,
                    demo=bool(getattr(ex, "demo_trading", False)),
                    order_id=str(res.get("order_id") or "") or None,
                ),
                kind="trade_open",
            )
        except Exception:
            pass


def _flat_close_from_exchange(
    ex: Any, pair: str, open_rate: float
) -> tuple[float, float | None]:
    """When position is already flat: Bybit avgExitPrice + closedPnl, else ticker."""
    sym = ft_pair_to_symbol(pair)
    close_rate = open_rate
    profit_abs: float | None = None
    got_exit = False
    try:
        closed = ex.latest_closed_pnl(sym)
        if closed:
            exit_px = float(closed.get("avgExitPrice") or 0)
            if exit_px > 0:
                close_rate = exit_px
                got_exit = True
            if closed.get("closedPnl") is not None:
                profit_abs = float(closed["closedPnl"])
    except Exception:
        pass
    if not got_exit:
        try:
            close_rate = float(ex.last_price(sym) or open_rate)
        except Exception:
            close_rate = open_rate
    return close_rate, profit_abs


def force_close_trade(user_id: str, trade_id: int) -> dict[str, Any]:
    """Manual UI close for shared-stack strategy trades (Bybit market + sqlite archive)."""
    tid = int(trade_id)
    uid = str(user_id)
    path = db_path(uid)
    if not path.is_file():
        raise FileNotFoundError(f"trades db missing for {uid}")
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM trades WHERE id=? AND is_open=1",
            (tid,),
        ).fetchone()
    if not row:
        raise KeyError(f"open trade #{tid} not found")
    pair = str(row["pair"] or "")
    is_short = bool(row["is_short"])
    open_rate = float(row["open_rate"] or 0)
    stake = float(row["stake_amount"] or 0)
    amount = float(row["amount"] or 0)
    tag = str(row["enter_tag"] or row["strategy"] or "")
    side = "short" if is_short else "long"

    ex = get_exchange(uid)
    if ex is None:
        raise RuntimeError("нет API-ключей Bybit")
    block = ex.trade_block_reason()
    if block:
        raise RuntimeError(block)

    close_rate = open_rate
    order_id = None
    closed_on_exchange = False
    try:
        res = ex.market_close(pair=pair, is_short=is_short, qty=amount if amount > 0 else None)
        closed_on_exchange = bool(res.get("closed"))
        close_rate = float(res.get("price") or open_rate)
        order_id = res.get("order_id")
        if not closed_on_exchange and res.get("reason") in ("no_position", "zero_size"):
            close_rate, _ = _flat_close_from_exchange(ex, pair, open_rate)
    except Exception as exc:
        msg = str(exc)
        # If exchange says flat / already closed, archive anyway
        low = msg.lower()
        if "110017" in msg or "position is zero" in low or "position idx" in low:
            close_rate, _ = _flat_close_from_exchange(ex, pair, open_rate)
        else:
            raise

    # Gross (price) PnL — UI applies fixed fee % for «С комиссией».
    if is_short and open_rate > 0:
        ratio = (open_rate - close_rate) / open_rate
    elif open_rate > 0:
        ratio = (close_rate - open_rate) / open_rate
    else:
        ratio = 0.0
    lev = float(row["leverage"] or 1) or 1.0
    ratio *= lev
    profit_abs = ratio * stake

    archived = archive_trade_in_db(
        path,
        tid,
        close_rate=close_rate,
        profit_abs=profit_abs,
        exit_reason="force_exit",
    )
    try:
        pair_entry_guard.release_pair("strategy", pair, side, tenant_id=uid)
    except Exception:
        pass

    log.info(
        "FORCE CLOSE user=%s #%s %s %s rate=%.6f pnl=%.4f order=%s exchange_closed=%s",
        uid,
        tid,
        side,
        pair,
        close_rate,
        profit_abs,
        order_id,
        closed_on_exchange,
    )
    try:
        import telegram_links as tg_links

        fee_info = tg_links.pnl_with_fees(
            profit_abs,
            amount=amount or None,
            open_rate=open_rate or None,
            close_rate=close_rate,
            stake_amount=stake or None,
            leverage=lev,
            profit_pct=ratio * 100,
        )
        tg_links.notify_user(
            uid,
            tg_links.format_exit_ru(
                pair=pair,
                strategy=trade_exit_monitor.base_enter_tag(tag),
                reason="force_exit",
                pnl_pct=ratio * 100,
                pnl_usdt=profit_abs,
                stake=stake or None,
                is_short=is_short,
                close_rate=close_rate,
                amount=amount or None,
                open_rate=open_rate or None,
                leverage=lev,
            ),
            kind="trade_close",
            pnl_pct=fee_info["net_pct"],
        )
    except Exception:
        pass
    return {
        "ok": True,
        "result": "force_exit",
        "trade_id": tid,
        "pair": pair,
        "close_rate": close_rate,
        "profit_abs": round(profit_abs, 6),
        "order_id": order_id,
        "closed_on_exchange": closed_on_exchange,
        "archived": archived,
    }


def count_open_trades(user_id: str, *, tag: str | None = None) -> int:
    path = db_path(user_id)
    if not path.is_file():
        return 0
    try:
        with sqlite3.connect(path) as conn:
            if tag:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE is_open=1 AND enter_tag LIKE ?",
                    (f"{tag}%",),
                ).fetchone()
            else:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE is_open=1"
                ).fetchone()
            return int(rows[0] if rows else 0)
    except sqlite3.Error:
        return 0


def count_open_on_pair(user_id: str, pair: str) -> int:
    path = db_path(user_id)
    if not path.is_file():
        return 0
    try:
        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE is_open=1 AND pair=?",
                (pair,),
            ).fetchone()
            return int(rows[0] if rows else 0)
    except sqlite3.Error:
        return 0


def exchange_has_position(user_id: str, pair: str, side: str) -> bool:
    ex = get_exchange(user_id)
    if ex is None:
        return False
    try:
        sym = ft_pair_to_symbol(pair)
        side_key = "sell" if side.lower() in ("short", "sell") else "buy"
        return (sym, side_key) in ex.positions_map()
    except Exception:
        return False


_SHARED_WL_CACHE: tuple[float, set[str]] | None = None


def shared_strategy_pairs() -> set[str]:
    """Pairs the shared signal-engine is allowed to watch (admin whitelist)."""
    global _SHARED_WL_CACHE
    now = time.monotonic()
    if _SHARED_WL_CACHE is not None and now - _SHARED_WL_CACHE[0] < 30.0:
        return _SHARED_WL_CACHE[1]
    pairs: set[str] = set()
    try:
        import user_prefs as up

        raw = up.load_strategy_whitelist().get("pairs") or []
        pairs = {str(p) for p in raw if str(p).strip()}
    except Exception:
        pairs = set()
    if not pairs:
        data = load_json(BASE / "user_data" / "config_signal_engine.json", {})
        raw = (data.get("exchange") or {}).get("pair_whitelist") or []
        pairs = {str(p) for p in raw if str(p).strip()}
    _SHARED_WL_CACHE = (now, pairs)
    return pairs


def selected_strategy_pairs(user_id: str, strategy_id: str) -> list[str] | None:
    """Tickers chosen for a main-panel strategy. None when the user has no pair map."""
    try:
        import user_prefs as up

        data = up.load_enabled_payload(user_id)
    except Exception:
        data = load_json(tenant_dir(user_id) / "enabled_strategies.json", {})
    if not isinstance(data, dict):
        return None
    raw = data.get("pairs")
    if not isinstance(raw, dict) or strategy_id not in raw:
        return None
    return [str(p) for p in (raw.get(strategy_id) or []) if str(p).strip()]


def _shared_whitelist_block(pair: str) -> str | None:
    wl = shared_strategy_pairs()
    if wl and pair not in wl:
        return f"пара {pair} не в общем списке пар"
    return None


def pair_block_reason(user_id: str, strategy_id: str, pair: str) -> str | None:
    """Test panel may use the whole shared list. Main panel only the chosen tickers.

    Users with no tickers chosen trade the shared whitelist. A non-empty ticker
    list, including on 1h strategies, limits entries to those pairs.
    """
    try:
        from htf1h_catalog import HTF1H_IDS
    except Exception:
        HTF1H_IDS = frozenset()
    selected = selected_strategy_pairs(user_id, strategy_id)
    if user_id != "admin" and not selected:
        return _shared_whitelist_block(pair)
    if strategy_id in HTF1H_IDS and selected:
        if pair not in selected:
            return f"пара {pair} не выбрана для {strategy_id}"
        return None
    if strategy_id in load_test_strategy_ids(user_id):
        return _shared_whitelist_block(pair)
    if selected is None:
        if not pair_allowed(user_id, pair):
            return f"пара {pair} не в whitelist пользователя"
        return None
    if pair not in selected:
        return f"пара {pair} не выбрана для {strategy_id}"
    return None


def pair_allowed(user_id: str, pair: str) -> bool:
    cfg = load_user_config(user_id)
    wl = (cfg.get("exchange") or {}).get("pair_whitelist") or []
    return pair in wl


def clear_exchange_cache(user_id: str | None = None) -> None:
    if user_id is None:
        _exchanges.clear()
    else:
        _exchanges.pop(str(user_id), None)


def insert_trade_row(
    user_id: str,
    *,
    pair: str,
    strategy_id: str,
    entry_tag: str,
    is_short: bool,
    open_rate: float,
    stake_amount: float,
    amount: float,
    leverage: float,
    stop_loss_ratio: float | None = None,
    take_profit_ratio: float | None = None,
    ml_meta: dict[str, Any] | None = None,
) -> int:
    path = ensure_trades_db(user_id)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    sl_price = None
    sl_pct = None
    if stop_loss_ratio is not None and stop_loss_ratio < 0 and open_rate > 0:
        # stop_loss_pct stays as stake-PnL ratio (TAG_RISK); price uses / leverage.
        sl_pct = float(stop_loss_ratio)
        move = UserBybitExchange.stake_risk_to_price_move(sl_pct, leverage)
        if is_short:
            sl_price = open_rate * (1 + move)
        else:
            sl_price = open_rate * (1 - move)
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                exchange, pair, base_currency, stake_currency, is_open, fee_open, fee_close,
                open_rate, open_rate_requested, open_trade_value,
                close_profit, close_profit_abs, stake_amount,
                amount, amount_requested, open_date,
                stop_loss, stop_loss_pct, initial_stop_loss, initial_stop_loss_pct,
                strategy, enter_tag, timeframe, trading_mode, leverage,
                is_short, is_stop_loss_trailing, realized_profit, interest_rate, record_version
            ) VALUES (
                'bybit', ?, 'USDT', 'USDT', 1, 0, 0,
                ?, ?, ?,
                NULL, NULL, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, '5m', 'futures', ?,
                ?, 0, 0, 0, 2
            )
            """,
            (
                pair,
                open_rate,
                open_rate,
                stake_amount,
                stake_amount,
                amount,
                amount,
                now,
                sl_price,
                sl_pct,
                sl_price,
                sl_pct,
                strategy_id,
                entry_tag,
                leverage,
                1 if is_short else 0,
            ),
        )
        trade_id = int(cur.lastrowid or 0)
        meta = dict(ml_meta or {})
        if stop_loss_ratio is not None:
            meta["stop_loss_ratio"] = float(stop_loss_ratio)
        if take_profit_ratio is not None and float(take_profit_ratio) > 0:
            meta["take_profit_ratio"] = float(take_profit_ratio)
        if trade_id and meta:
            _insert_trade_custom_data(conn, trade_id, meta, now=now)
        conn.commit()
    return trade_id


def _insert_trade_custom_data(
    conn: sqlite3.Connection,
    trade_id: int,
    meta: dict[str, Any],
    *,
    now: str,
) -> None:
    rows: list[tuple[Any, ...]] = []
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, bool):
            cd_type, cd_value = "bool", "true" if value else "false"
        elif isinstance(value, int) and not isinstance(value, bool):
            cd_type, cd_value = "int", str(value)
        elif isinstance(value, float):
            cd_type, cd_value = "float", str(value)
        else:
            cd_type, cd_value = "str", str(value)
        rows.append((trade_id, str(key), cd_type, cd_value, now))
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO trade_custom_data
            (ft_trade_id, cd_key, cd_type, cd_value, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, NULL)
        """,
        rows,
    )


def _ml_meta_from_event(user_id: str, ev: dict[str, Any]) -> dict[str, Any]:
    """Build trade_custom_data payload for UI ML confidence columns."""
    sid = str(ev.get("strategy_id") or "")
    scenario = ev.get("scenario") if isinstance(ev.get("scenario"), dict) else None
    min_conf = _ml_min_conf(user_id, sid, scenario)
    raw = ev.get("ml") if isinstance(ev.get("ml"), dict) else {}
    out: dict[str, Any] = {"ml_min_confidence": float(min_conf)}
    if raw:
        predicted = raw.get("predicted")
        if predicted is not None:
            out["ml_predicted"] = str(predicted)
        conf = raw.get("confidence_profit")
        if conf is not None:
            try:
                conf_f = float(conf)
                out["ml_confidence"] = conf_f
                out["ml_gate_confidence"] = conf_f
            except (TypeError, ValueError):
                pass
    return out


def _reconcile_user_positions(user_id: str) -> None:
    try:
        # New tenants often have no sqlite yet — create before import.
        ensure_trades_db(user_id)
        from import_exchange_positions import import_positions

        result = import_positions(user_id)
        n = int(result.get("imported_count") or 0)
        if n:
            log.warning("reconcile imported %s open position(s) for user=%s", n, user_id)
    except Exception as exc:
        log.error("reconcile failed user=%s: %s", user_id, exc)


def _ohlcv_for_pair(pair: str) -> Any:
    now = time.monotonic()
    cached = _ohlcv_cache.get(pair)
    if cached and now - cached[0] < OHLCV_CACHE_TTL:
        return cached[1]
    df = None
    try:
        sym = ft_pair_to_symbol(pair)
        rows = fetch_public_klines(sym, interval="5", limit=250)
        df = trade_exit_monitor.klines_to_df(rows)
    except Exception as exc:
        log.warning("ohlcv fetch failed %s: %s", pair, exc)
    _ohlcv_cache[pair] = (now, df)
    return df


def _ml_min_conf(user_id: str, strategy_id: str, scenario: dict[str, Any] | None = None) -> float:
    if get_live_ml_gate is not None and scenario:
        try:
            _, min_conf = get_live_ml_gate()._resolve_gate_rules(scenario)
            return float(min_conf)
        except Exception:
            pass
    return float(load_ml_confidence(user_id).get(strategy_id, 0.55))


def _ml_from_signal(ev: dict[str, Any], min_conf: float) -> bool | None:
    raw = ev.get("ml")
    if not isinstance(raw, dict) or not raw:
        return None
    if not raw.get("ready", True):
        return False
    predicted = str(raw.get("predicted") or "")
    conf = float(raw.get("confidence_profit") or 0)
    return predicted == "profit" and conf >= min_conf


def ml_allows(user_id: str, ev: dict[str, Any]) -> bool:
    sid = str(ev.get("strategy_id") or "")
    scenario = ev.get("scenario") or {}
    min_conf = _ml_min_conf(user_id, sid, scenario if isinstance(scenario, dict) else None)
    from_signal = _ml_from_signal(ev, min_conf)
    if from_signal is not None:
        return from_signal
    scenario = ev.get("scenario") or {}
    if not scenario:
        return True
    cfg = load_user_config(user_id)
    stake = _strategy_stake(user_id, sid, cfg)
    side = str(ev.get("side") or "long")
    rate = float(ev.get("rate") or 0)
    tag = trade_exit_monitor.base_enter_tag(sid)
    sl, tp = trade_exit_monitor.risk_thresholds(
        user_id,
        tag,
        tenant_dir=tenant_dir,
        load_json=load_json,
        load_user_config=load_user_config,
    )
    roi = {"0": tp}
    ohlcv_df = _ohlcv_for_pair(str(ev.get("pair") or ""))
    if get_live_ml_gate is not None:
        try:
            gate = get_live_ml_gate()
            ml = gate.score_trade_entry(
                scenario=scenario,
                pair=str(ev.get("pair") or ""),
                rate=rate,
                side=side,
                current_time=datetime.now(UTC),
                stake_usdt=stake,
                stoploss=sl,
                minimal_roi=roi,
                timeframe="5m",
                ohlcv_df=ohlcv_df,
            )
            predicted = str(ml.get("predicted") or "")
            conf = float(ml.get("confidence_profit") or 0)
            if predicted != "profit" or conf < min_conf:
                return False
            return True
        except Exception as exc:
            log.warning("ML score user=%s pair=%s: %s", user_id, ev.get("pair"), exc)
    if allow_trade_entry is None:
        return True
    try:
        return bool(
            allow_trade_entry(
                scenario=scenario,
                pair=str(ev.get("pair") or ""),
                rate=rate,
                side=side,
                current_time=datetime.now(UTC),
                stake_usdt=stake,
                stoploss=sl,
                minimal_roi=roi,
                timeframe="5m",
                ohlcv_df=ohlcv_df,
            )
        )
    except Exception as exc:
        log.warning("ML gate error user=%s pair=%s: %s", user_id, ev.get("pair"), exc)
        return True


def _close_book_pair(user_id: str, pair: str, *, tag: str = "Deepseek50", bot: str = "llm50") -> None:
    path = db_path(user_id)
    if not path.is_file():
        return
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, is_short FROM trades
                WHERE is_open=1 AND pair=? AND (
                    enter_tag LIKE ? OR strategy=?
                )
                ORDER BY id DESC LIMIT 1
                """,
                (pair, f"{tag}%", tag),
            ).fetchone()
    except sqlite3.Error as exc:
        log.warning("%s close lookup user=%s %s: %s", bot, user_id, pair, exc)
        return
    if row is None:
        log.info("%s CLOSE skip user=%s %s — нет открытой сделки", bot, user_id, pair)
        return
    side = "short" if row["is_short"] else "long"
    force_close_trade(user_id, int(row["id"]))
    try:
        pair_entry_guard.release_pair(bot, pair, side, tenant_id=user_id)
    except Exception:
        pass


def process_event(ev: dict[str, Any]) -> None:
    eid = str(ev.get("id") or "")
    if not eid or eid in _processed:
        return
    if ev.get("event") == "exit_signal" and str(ev.get("bot") or "") in {"llm50", "cursor50"}:
        book = str(ev.get("bot") or "")
        tag = "Cursor50" if book == "cursor50" else "Deepseek50"
        target = str(ev.get("user_id") or "")
        users = user_trading.list_active_traders(book)
        if target:
            users = [uid for uid in users if uid == target]
        for uid in users:
            try:
                _close_book_pair(uid, str(ev.get("pair") or ""), tag=tag, bot=book)
            except Exception as exc:
                log.exception("%s close user=%s: %s", book, uid, exc)
        _processed.add(eid)
        return
    if ev.get("event") != "entry_signal":
        _processed.add(eid)
        return
    bot = str(ev.get("bot") or "strategy")
    if bot not in ("strategy", "llm", "llm50", "cursor", "cursor50", "price_action"):
        bot = "strategy"
    users = user_trading.list_active_traders(bot)
    target = str(ev.get("user_id") or "")
    if bot in {"llm50", "cursor50"} and target:
        users = [uid for uid in users if uid == target]
    for uid in users:
        try:
            execute_for_user(uid, ev)
        except Exception as exc:
            log.exception("execute user=%s signal=%s: %s", uid, eid, exc)
    _processed.add(eid)


def _exit_ctx() -> dict[str, Any]:
    return {
        "get_exchange": get_exchange,
        "db_path": db_path,
        "tenant_dir": tenant_dir,
        "load_json": load_json,
        "load_user_config": load_user_config,
        "archive_trade_in_db": archive_trade_in_db,
        "ft_pair_to_symbol": ft_pair_to_symbol,
        "fetch_public_klines": fetch_public_klines,
        "count_open_trades": count_open_trades,
    }


def _log_idle_status(traders: list[str]) -> None:
    """Periodic why-nothing-trades summary (every ~60s) — visible in UI → Логи → Исполнитель."""
    if not traders:
        log.warning(
            "STATUS: нет активных трейдеров — включите торговлю и API-ключи в Настройках"
        )
        return
    feed = signal_bus.feed_path()
    feed_ok = Path(feed).is_file() if feed else False
    feed_lines = 0
    try:
        if feed_ok:
            feed_lines = sum(1 for _ in Path(feed).open(encoding="utf-8", errors="replace"))
    except OSError:
        feed_ok = False
    skips = _skip_summary(300.0)
    for uid in traders:
        ex = get_exchange(uid)
        if ex is None:
            log.warning("STATUS user=%s: нет Bybit API-ключей — сделки не открываются", uid)
            continue
        block = None
        try:
            block = ex.trade_block_reason()
        except Exception as exc:
            block = f"ошибка проверки ключа: {exc}"
        if block:
            log.error("STATUS user=%s: сделки не открываются — %s", uid, block)
            continue
        limits = load_limits(uid)
        main_open = count_open_by_panel(uid, test=False)
        test_open = count_open_by_panel(uid, test=True)
        log.info(
            "STATUS user=%s: ключ OK demo=%s open_main=%s/%s open_test=%s/%s feed=%s "
            "(lines=%s) entry_ok=%s — %s. "
            "Стратегии: signal→executor; если мало ENTRY — смотрите SKIP (чаще ML gate).",
            uid,
            ex.demo_trading,
            main_open,
            limits.get("max_open_trades"),
            test_open,
            limits.get("test_max_open_trades"),
            "ok" if feed_ok else "MISSING",
            feed_lines,
            _recent_entry_ok,
            skips,
        )


def main() -> None:
    log.info("trade executor started; feed=%s exit_interval=%ss", signal_bus.feed_path(), EXIT_INTERVAL_SEC)
    traders = user_trading.list_active_traders("strategy")
    log.info("active traders for strategy: %s", traders or "(none)")
    if not traders:
        log.warning(
            "нет активных трейдеров — включите торговлю в Настройках и задайте Bybit API ключи"
        )
    for uid in traders:
        try:
            ensure_trades_db(uid)
        except Exception as exc:
            log.warning("ensure db user=%s: %s", uid, exc)
        ex = get_exchange(uid)
        if ex is None:
            log.warning("user=%s: API ключи не загружены", uid)
        else:
            log.info(
                "user=%s: exchange ok demo=%s base=%s",
                uid,
                getattr(ex, "demo_trading", False),
                getattr(ex, "api_base", "?"),
            )
            try:
                block = ex.trade_block_reason()
                if block:
                    log.error("user=%s: ТОРГОВЛЯ ЗАБЛОКИРОВАНА — %s", uid, block)
                else:
                    info = ex.query_api_key()
                    log.info(
                        "user=%s: API key ok note=%r readOnly=%s perms.ContractTrade=%s",
                        uid,
                        info.get("note"),
                        info.get("readOnly"),
                        (info.get("permissions") or {}).get("ContractTrade"),
                    )
            except Exception as exc:
                log.warning("user=%s: не удалось проверить права API-ключа: %s", uid, exc)
        _reconcile_user_positions(uid)
    last_line = signal_bus.read_offset()
    log.info("signal feed offset=%s", last_line)
    last_exit = 0.0
    last_status = 0.0
    exit_ctx = _exit_ctx()
    while _running:
        _touch_heartbeat()
        # Cap per loop but advance offset only through processed events —
        # truncating a full read used to skip the rest of the backlog forever.
        events, last_line = signal_bus.tail_new_events(
            last_line, limit=MAX_EVENTS_PER_LOOP
        )
        for ev in events:
            process_event(ev)
        if events:
            signal_bus.write_offset(last_line)
            log.info("processed %s signal(s), offset=%s", len(events), last_line)
        now = time.monotonic()
        if now - last_exit >= EXIT_INTERVAL_SEC:
            trade_exit_monitor.monitor_all_users(exit_ctx)
            for uid in user_trading.list_active_traders("strategy"):
                try:
                    ensure_trades_db(uid)
                except Exception as exc:
                    log.warning("ensure db user=%s: %s", uid, exc)
                _reconcile_user_positions(uid)
            last_exit = now
        if now - last_status >= 60.0:
            _log_idle_status(traders if traders else user_trading.list_active_traders("strategy"))
            last_status = now
        time.sleep(1.0)
    signal_bus.write_offset(last_line)
    log.info("trade executor stopped at line=%s", last_line)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Execute shared signal-engine entries per user Bybit keys."""
from __future__ import annotations

import json
import logging
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
    _fh = logging.FileHandler(_log_dir / "trade-executor.log", encoding="utf-8")
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


def load_enabled(user_id: str) -> dict[str, bool]:
    data = load_json(tenant_dir(user_id) / "enabled_strategies.json", {})
    enabled = data.get("enabled") if isinstance(data, dict) else {}
    if not isinstance(enabled, dict):
        return {}
    return {str(k): bool(v) for k, v in enabled.items()}


def load_ml_confidence(user_id: str) -> dict[str, float]:
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
    return load_json(tenant_dir(user_id) / "test_strategy_settings.json", {})


def load_test_strategy_ids(user_id: str) -> set[str]:
    """Best-effort test-panel strategy ids (fallback list + placement)."""
    fallback = {
        "PsaraFlipTestStrategy",
        "AtrChannelBreakoutTestStrategy",
        "AdxMomentumTestStrategy",
        "SupertrendTestStrategy",
        "CmfZeroCrossTestStrategy",
        "ScalpEmaCrossTestStrategy",
        "ChaikinOscTestStrategy",
        "DonchianBreakoutTestStrategy",
        "PpoSignalTestStrategy",
        "DonchianAdxVolComboTestStrategy",
        "ObvEmaCrossTestStrategy",
        "ElderRayTestStrategy",
        "AltVolumeBreakoutTestStrategy",
        "BollingerRsiTestStrategy",
        "MacdEmaTestStrategy",
        "GruBarrierStrategy",
    }
    path = tenant_dir(user_id) / "strategy_ui_placement.json"
    data = load_json(path, {})
    main = {str(x) for x in (data.get("main") or [])} if isinstance(data, dict) else set()
    return {sid for sid in fallback if sid not in main}


def load_limits(user_id: str) -> dict[str, int]:
    cfg = load_user_config(user_id)
    max_open = int(cfg.get("max_open_trades") or 0)
    # Test-block concurrent cap must fit under the strategy-bot ceiling.
    test = load_test_settings(user_id)
    try:
        test_max = max(0, int(test.get("max_open_trades") or 0))
    except (TypeError, ValueError):
        test_max = 0
    if test_max > max_open:
        max_open = test_max
    per_path = tenant_dir(user_id) / "max_open_trades_per_strategy.json"
    per = 0
    data = load_json(per_path, {})
    if isinstance(data, dict):
        try:
            per = max(0, int(data.get("value") or 0))
        except (TypeError, ValueError):
            per = 0
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
            key = (os.environ.get("BYBIT_API_KEY") or "").strip()
            secret = (os.environ.get("BYBIT_API_SECRET") or "").strip()
            demo_trading = str(
                os.environ.get("BYBIT_DEMO_TRADING")
                or os.environ.get("CTENGINE__EXCHANGE__DEMO_TRADING")
                or ""
            ).strip().lower() in ("1", "true", "t", "yes", "y", "on")
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


def skip_reason(user_id: str, ev: dict[str, Any]) -> str | None:
    """Human-readable why this signal will not open a trade (None = allowed)."""
    sid = str(ev.get("strategy_id") or "")
    pair = str(ev.get("pair") or "")
    side = str(ev.get("side") or "long")
    if not sid:
        return "нет strategy_id в сигнале"
    enabled = load_enabled(user_id)
    if not enabled:
        return f"пустой enabled_strategies для {user_id}"
    if not enabled.get(sid, False):
        return f"стратегия {sid} выключена у пользователя"
    if not pair:
        return "нет pair в сигнале"
    if not pair_allowed(user_id, pair):
        return f"пара {pair} не в whitelist пользователя"
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
    if limits["max_open_trades"] <= 0:
        return "max_open_trades=0 (лимит сделок выключен/ноль)"
    open_n = count_open_trades(user_id)
    if open_n >= limits["max_open_trades"]:
        return f"лимит открытых сделок {open_n}/{limits['max_open_trades']}"
    test_ids = load_test_strategy_ids(user_id)
    if sid in test_ids:
        test_cap = int(limits.get("test_max_open_trades") or 0)
        if test_cap > 0:
            # Count only open test-panel trades toward the test-block ceiling.
            test_open = 0
            for tid in test_ids:
                test_open += count_open_trades(user_id, tag=tid)
            if test_open >= test_cap:
                return f"лимит тестовых стратегий {test_open}/{test_cap}"
        per = int(limits.get("test_max_per_strategy") or 0)
    else:
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
        ok_claim, claim_reason = pair_entry_guard.claim_pair(
            "strategy",
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
        stake = float(cfg.get("stake_amount") or 5)
        leverage = 3.0
        tag = str(ev.get("entry_tag") or ev.get("strategy_id") or "")
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
            pair_entry_guard.release_pair("strategy", pair, side, tenant_id=user_id)
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
            )
        except Exception:
            pass


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
    ml_meta: dict[str, Any] | None = None,
) -> int:
    path = ensure_trades_db(user_id)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    sl_price = None
    sl_pct = None
    if stop_loss_ratio is not None and stop_loss_ratio < 0 and open_rate > 0:
        sl_pct = float(stop_loss_ratio)
        if is_short:
            sl_price = open_rate * (1 + abs(sl_pct))
        else:
            sl_price = open_rate * (1 - abs(sl_pct))
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
                0, 0, ?,
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
        if trade_id and ml_meta:
            _insert_trade_custom_data(conn, trade_id, ml_meta, now=now)
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
    stake = float(cfg.get("stake_amount") or 5)
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


def process_event(ev: dict[str, Any]) -> None:
    eid = str(ev.get("id") or "")
    if not eid or eid in _processed:
        return
    if ev.get("event") != "entry_signal":
        _processed.add(eid)
        return
    users = user_trading.list_active_traders("strategy")
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
        open_n = count_open_trades(uid)
        log.info(
            "STATUS user=%s: ключ OK demo=%s open=%s/%s feed=%s (lines=%s) entry_ok=%s — %s. "
            "Стратегии: signal→executor; если мало ENTRY — смотрите SKIP (чаще ML gate).",
            uid,
            ex.demo_trading,
            open_n,
            limits.get("max_open_trades"),
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
        events, last_line = signal_bus.tail_new_events(last_line)
        if len(events) > MAX_EVENTS_PER_LOOP:
            log.warning("feed backlog %s events — processing first %s", len(events), MAX_EVENTS_PER_LOOP)
            events = events[:MAX_EVENTS_PER_LOOP]
        for ev in events:
            process_event(ev)
        if events:
            signal_bus.write_offset(last_line)
            log.info("processed %s signal(s), offset=%s", len(events), last_line)
        now = time.monotonic()
        if now - last_exit >= EXIT_INTERVAL_SEC:
            trade_exit_monitor.monitor_all_users(exit_ctx)
            for uid in user_trading.list_active_traders("strategy"):
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

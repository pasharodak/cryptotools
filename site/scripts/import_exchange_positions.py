#!/usr/bin/env python3
"""Import live Bybit positions into tenant strategy DB (exchange-only rows)."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
SCRIPTS = BASE / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import signal_bus  # noqa: E402
import tenant_manager as tm  # noqa: E402
import trade_exit_monitor  # noqa: E402
from bybit_grid_manager import symbol_to_ft_pair  # noqa: E402
from user_exchange import UserBybitExchange, ft_pair_to_symbol  # noqa: E402


def _load_exchange(user_id: str) -> UserBybitExchange:
    if user_id == "admin":
        blob = tm._load_secrets_blob("admin")
        key = str(blob.get("bybit_api_key") or "").strip()
        secret = str(blob.get("bybit_api_secret") or "").strip()
        demo = tm._as_bool(blob.get("bybit_demo_trading"))
        if not key or not secret:
            raise RuntimeError("no secrets for admin")
        return UserBybitExchange(key, secret, demo_trading=demo)
    sec = tm.load_user_secrets(user_id)
    if not sec:
        raise RuntimeError(f"no secrets for {user_id}")
    demo = bool(sec.get("bybit_demo_trading")) if isinstance(sec.get("bybit_demo_trading"), bool) else str(
        sec.get("bybit_demo_trading") or ""
    ).strip().lower() in ("1", "true", "t", "yes", "y", "on")
    return UserBybitExchange(
        sec["bybit_api_key"],
        sec["bybit_api_secret"],
        demo_trading=demo,
    )


def _db_path(user_id: str) -> Path:
    if user_id == "admin":
        cfg_path = tm.BASE / "user_data" / "config_strategy.json"
    else:
        cfg_path = tm.tenant_user_data(user_id) / "config_strategy.json"
    raw = "sqlite:///tradesv3-strategy.sqlite"
    if cfg_path.is_file():
        import json as _json

        raw = str(_json.loads(cfg_path.read_text(encoding="utf-8")).get("db_url") or raw)
    rel = raw.replace("sqlite:///", "").lstrip("/")
    path = Path(rel)
    if path.is_absolute():
        return path
    name = Path(rel).name
    if user_id == "admin":
        ud = tm.BASE / "user_data" / name
        legacy = BASE / rel
        if ud.is_file() or not legacy.is_file():
            return ud
        return legacy
    return BASE / rel


def _open_db_keys(conn: sqlite3.Connection) -> set[tuple[str, int]]:
    rows = conn.execute("SELECT pair, is_short FROM trades WHERE is_open=1").fetchall()
    return {(str(r[0]), int(r[1])) for r in rows}


def _last_feed_strategy(pair: str) -> str | None:
    sid = None
    for ev in signal_bus.iter_events():
        if ev.get("event") == "entry_signal" and ev.get("pair") == pair:
            sid = str(ev.get("strategy_id") or "")
    return sid or None


def _tenant_dir(user_id: str) -> Path:
    return tm.tenant_user_data(user_id)


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _load_user_config(user_id: str) -> dict[str, Any]:
    cfg_path = _tenant_dir(user_id) / "config_strategy.json"
    return _load_json(cfg_path, {})


def _insert_open(
    conn: sqlite3.Connection,
    *,
    pair: str,
    is_short: bool,
    open_rate: float,
    stake_amount: float,
    amount: float,
    leverage: float,
    strategy_id: str,
    stop_loss_ratio: float | None = None,
    open_date: str | None = None,
) -> int:
    now = open_date or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    sl_price = None
    sl_pct = None
    if stop_loss_ratio is not None and stop_loss_ratio < 0 and open_rate > 0:
        sl_pct = float(stop_loss_ratio)
        if is_short:
            sl_price = open_rate * (1 + abs(sl_pct))
        else:
            sl_price = open_rate * (1 - abs(sl_pct))
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
            strategy_id,
            leverage,
            1 if is_short else 0,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def import_positions(
    user_id: str,
    *,
    strategy_by_pair: dict[str, str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ex = _load_exchange(user_id)
    db = _db_path(user_id)
    if not db.is_file():
        raise RuntimeError(f"db missing: {db}")

    strategy_by_pair = dict(strategy_by_pair or {})
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    conn = sqlite3.connect(db)
    try:
        existing = _open_db_keys(conn)
        for (symbol, side_key), pos in ex.positions_map().items():
            pair = symbol_to_ft_pair(symbol)
            is_short = side_key == "sell"
            key = (pair, 1 if is_short else 0)
            if key in existing:
                skipped.append({"pair": pair, "reason": "already_in_db"})
                continue

            size = float(pos.get("size") or 0)
            avg = float(pos.get("avgPrice") or 0)
            lev = float(pos.get("leverage") or 3)
            im = float(pos.get("positionIM") or 0)
            if size <= 0 or avg <= 0:
                skipped.append({"pair": pair, "reason": "invalid_size_or_price"})
                continue

            stake = im if im > 0 else (size * avg / max(lev, 1))
            sid = strategy_by_pair.get(pair) or _last_feed_strategy(pair) or "ImportedPosition"
            tag = trade_exit_monitor.base_enter_tag(sid)
            sl, _tp = trade_exit_monitor.risk_thresholds(
                user_id,
                tag,
                tenant_dir=_tenant_dir,
                load_json=_load_json,
                load_user_config=_load_user_config,
            )
            created = pos.get("createdTime")
            open_date = None
            if created:
                try:
                    open_date = datetime.fromtimestamp(int(created) / 1000, tz=UTC).strftime(
                        "%Y-%m-%d %H:%M:%S.%f"
                    )
                except (TypeError, ValueError):
                    open_date = None

            row = {
                "pair": pair,
                "side": "short" if is_short else "long",
                "strategy_id": sid,
                "open_rate": avg,
                "amount": size,
                "stake_amount": stake,
                "leverage": lev,
            }
            if dry_run:
                imported.append({**row, "dry_run": True})
                continue

            trade_id = _insert_open(
                conn,
                pair=pair,
                is_short=is_short,
                open_rate=avg,
                stake_amount=stake,
                amount=size,
                leverage=lev,
                strategy_id=sid,
                stop_loss_ratio=sl,
                open_date=open_date,
            )
            imported.append({**row, "trade_id": trade_id})
            existing.add(key)
    finally:
        conn.close()

    return {
        "user_id": user_id,
        "imported": imported,
        "skipped": skipped,
        "imported_count": len(imported),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Import Bybit positions into tenant DB")
    ap.add_argument("user_id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    result = import_positions(args.user_id, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"imported {result['imported_count']} positions for {args.user_id}")
        for row in result["imported"]:
            print(f"  #{row.get('trade_id')} {row['pair']} {row['side']} {row['strategy_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

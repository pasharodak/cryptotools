#!/usr/bin/env python3
"""Reconcile open bot trades with Bybit live positions."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE = Path(os.environ.get("CT_BASE", "/home/cryptotools/app"))
DEFAULT_ENV = Path(os.environ.get("CT_ENV", "/home/cryptotools/.cryptotools.env"))

BOTS: dict[str, dict[str, Any]] = {
    "finder": {
        "config": "user_data/config.json",
        "port": 8080,
    },
    "strategy": {
        "config": "user_data/config_strategy.json",
        "port": 8081,
    },
    "grid": {
        "config": "user_data/config_grid.json",
        "port": 8082,
    },
}


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def db_path_for_config(base: Path, config_rel: str) -> Path:
    cfg = json.loads((base / config_rel).read_text(encoding="utf-8"))
    raw = cfg.get("db_url", "sqlite:///tradesv3.sqlite")
    name = raw.split("///")[-1]
    return base / name


def ft_pair_to_bybit(pair: str) -> str:
    return pair.split(":")[0].replace("/", "")


def ft_side_key(is_short: int | bool) -> str:
    return "sell" if is_short else "buy"


def bybit_side_key(side: str) -> str:
    return "sell" if side.lower() in ("sell", "short") else "buy"


def load_open_trades(base: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bot, meta in BOTS.items():
        db = db_path_for_config(base, meta["config"])
        if not db.exists():
            continue
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        for t in conn.execute(
            """
            SELECT id, pair, is_short, amount, open_rate, stake_amount, open_date, strategy, enter_tag
            FROM trades WHERE is_open = 1 ORDER BY id
            """
        ):
            item = dict(t)
            item["bot"] = bot
            item["db"] = str(db)
            item["port"] = meta["port"]
            rows.append(item)
        conn.close()
    return rows


def bybit_positions(key: str, secret: str) -> list[dict[str, Any]]:
    base = "https://api.bybit.com"
    recv = 60000
    positions: list[dict[str, Any]] = []
    cursor = None
    while True:
        ts = str(int(time.time() * 1000))
        params: dict[str, str] = {"category": "linear", "settleCoin": "USDT", "limit": "200"}
        if cursor:
            params["cursor"] = cursor
        qs = urllib.parse.urlencode(sorted(params.items()))
        sign = hmac.new(
            secret.encode(),
            (ts + key + str(recv) + qs).encode(),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-BAPI-API-KEY": key,
            "X-BAPI-SIGN": sign,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": str(recv),
        }
        r = requests.get(f"{base}/v5/position/list?{qs}", headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("retCode") != 0:
            raise RuntimeError(data)
        for p in data.get("result", {}).get("list", []):
            if float(p.get("size") or 0) > 0:
                positions.append(p)
        cursor = data.get("result", {}).get("nextPageCursor")
        if not cursor:
            break
    return positions


def bybit_cancel_order(key: str, secret: str, symbol: str, order_id: str) -> dict[str, Any]:
    recv = 60000
    ts = str(int(time.time() * 1000))
    body = json.dumps({"category": "linear", "symbol": symbol, "orderId": order_id})
    sign = hmac.new(
        secret.encode(),
        (ts + key + str(recv) + body).encode(),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "X-BAPI-API-KEY": key,
        "X-BAPI-SIGN": sign,
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": str(recv),
        "Content-Type": "application/json",
    }
    r = requests.post(
        "https://api.bybit.com/v5/order/cancel",
        headers=headers,
        data=body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _auth_header(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def api_request(
    port: int,
    user: str,
    password: str,
    method: str,
    path: str,
    payload: dict | None = None,
) -> Any:
    url = f"http://127.0.0.1:{port}/api/v1{path}"
    data = None
    headers = {"Authorization": _auth_header(user, password)}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw.decode()) if raw else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {path}: {body}") from exc


def reconcile(
    base: Path | None = None,
    env_path: Path | None = None,
) -> dict[str, Any]:
    base = base or DEFAULT_BASE
    env = load_env(env_path or DEFAULT_ENV)
    key = env.get("BYBIT_API_KEY", "")
    secret = env.get("BYBIT_API_SECRET", "")

    open_trades = load_open_trades(base)
    positions = bybit_positions(key, secret) if key and secret else []

    bybit_map = {
        (p["symbol"], bybit_side_key(p.get("side", ""))): p for p in positions
    }
    occupied_pairs = sorted({t["pair"] for t in open_trades})
    duplicate_pairs = sorted(
        p for p in occupied_pairs if sum(1 for t in open_trades if t["pair"] == p) > 1
    )

    ghosts: list[dict[str, Any]] = []
    matched: list[dict[str, Any]] = []
    for t in open_trades:
        sym = ft_pair_to_bybit(t["pair"])
        side = ft_side_key(t["is_short"])
        entry = {
            "bot": t["bot"],
            "trade_id": t["id"],
            "pair": t["pair"],
            "side": side,
            "port": t["port"],
        }
        if (sym, side) in bybit_map:
            matched.append(entry)
        else:
            ghosts.append(entry)

    exchange_only: list[dict[str, Any]] = []
    ft_keys = {
        (ft_pair_to_bybit(t["pair"]), ft_side_key(t["is_short"])) for t in open_trades
    }
    for p in positions:
        sym = p["symbol"]
        side = bybit_side_key(p.get("side", ""))
        if (sym, side) not in ft_keys:
            exchange_only.append(
                {
                    "symbol": sym,
                    "side": side,
                    "size": float(p.get("size") or 0),
                    "avgPrice": p.get("avgPrice"),
                }
            )

    return {
        "ok": len(ghosts) == 0 and len(exchange_only) == 0,
        "ft_open_count": len(open_trades),
        "bybit_position_count": len(positions),
        "ghost_count": len(ghosts),
        "exchange_only_count": len(exchange_only),
        "ghosts": ghosts,
        "matched": matched,
        "exchange_only": exchange_only,
        "duplicate_pairs": duplicate_pairs,
        "bybit_positions": [
            {
                "symbol": p.get("symbol"),
                "side": p.get("side"),
                "size": p.get("size"),
                "avgPrice": p.get("avgPrice"),
            }
            for p in positions
        ],
    }


def bybit_last_price(key: str, secret: str, symbol: str) -> float | None:
    """Public ticker — auth unused but kept for signature consistency."""
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": symbol},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        rows = data.get("result", {}).get("list") or []
        if not rows:
            return None
        last = rows[0].get("lastPrice")
        return float(last) if last is not None else None
    except Exception as exc:
        logger.warning("ticker %s: %s", symbol, exc)
        return None


def bybit_closed_pnl_near(
    key: str,
    secret: str,
    symbol: str,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict[str, Any] | None:
    """Latest closed-PnL row for symbol (best-effort)."""
    recv = 60000
    ts = str(int(time.time() * 1000))
    params: dict[str, str] = {
        "category": "linear",
        "symbol": symbol,
        "limit": "50",
    }
    if start_ms:
        params["startTime"] = str(int(start_ms))
    if end_ms:
        params["endTime"] = str(int(end_ms))
    qs = urllib.parse.urlencode(sorted(params.items()))
    sign = hmac.new(
        secret.encode(),
        (ts + key + str(recv) + qs).encode(),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "X-BAPI-API-KEY": key,
        "X-BAPI-SIGN": sign,
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": str(recv),
    }
    try:
        r = requests.get(
            f"https://api.bybit.com/v5/position/closed-pnl?{qs}",
            headers=headers,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("retCode") != 0:
            return None
        rows = data.get("result", {}).get("list") or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.warning("closed-pnl %s: %s", symbol, exc)
        return None


def _estimate_profit(
    *,
    open_rate: float,
    close_rate: float,
    amount: float,
    is_short: bool,
) -> tuple[float, float]:
    if not open_rate or not amount:
        return 0.0, 0.0
    if is_short:
        profit_abs = amount * (open_rate - close_rate)
        profit_ratio = (open_rate - close_rate) / open_rate
    else:
        profit_abs = amount * (close_rate - open_rate)
        profit_ratio = (close_rate - open_rate) / open_rate
    return float(profit_ratio), float(profit_abs)


def archive_trade_in_db(
    db_path: Path,
    trade_id: int,
    *,
    close_rate: float | None = None,
    profit_abs: float | None = None,
    exit_reason: str = "reconcile",
) -> dict[str, Any]:
    """
    Close an open trade in sqlite without deleting it — keeps history for the UI.
    Used when the exchange position is already flat and forceexit cannot fill an order.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM trades WHERE id = ? AND is_open = 1",
            (trade_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"open trade #{trade_id} not found in {db_path.name}"}

        open_rate = float(row["open_rate"] or 0)
        amount = float(row["amount"] or 0)
        is_short = bool(row["is_short"])
        rate = float(close_rate) if close_rate and close_rate > 0 else open_rate

        if profit_abs is None:
            profit_ratio, profit_abs_calc = _estimate_profit(
                open_rate=open_rate,
                close_rate=rate,
                amount=amount,
                is_short=is_short,
            )
        else:
            profit_abs_calc = float(profit_abs)
            stake = float(row["stake_amount"] or 0) or 1.0
            profit_ratio = (profit_abs_calc / stake) if stake else 0.0

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """
            UPDATE trades SET
                is_open = 0,
                close_date = ?,
                close_rate = ?,
                close_profit = ?,
                close_profit_abs = ?,
                realized_profit = ?,
                exit_reason = ?,
                exit_order_status = COALESCE(exit_order_status, 'closed')
            WHERE id = ? AND is_open = 1
            """,
            (
                now,
                rate,
                profit_ratio,
                profit_abs_calc,
                profit_abs_calc,
                exit_reason,
                trade_id,
            ),
        )
        # Cancel lingering open/stoploss order rows so the bot stops touching them.
        conn.execute(
            """
            UPDATE orders SET status = 'canceled'
            WHERE ft_trade_id = ? AND lower(coalesce(status, '')) IN ('open', 'new', 'partially_filled', '')
            """,
            (trade_id,),
        )
        conn.commit()
        return {
            "ok": True,
            "action": "archived",
            "trade_id": trade_id,
            "pair": row["pair"],
            "close_rate": rate,
            "close_profit_abs": profit_abs_calc,
            "exit_reason": exit_reason,
        }
    finally:
        conn.close()


def db_path_for_bot(base: Path, bot: str) -> Path | None:
    meta = BOTS.get(bot)
    if not meta:
        return None
    path = db_path_for_config(base, meta["config"])
    return path if path.exists() else None


def fix_ghost_trade(
    ghost: dict[str, Any],
    user: str,
    password: str,
    *,
    base: Path | None = None,
    key: str = "",
    secret: str = "",
) -> dict[str, Any]:
    port = int(ghost["port"])
    trade_id = int(ghost["trade_id"])
    bot = ghost["bot"]
    pair = ghost["pair"]
    out: dict[str, Any] = {"bot": bot, "trade_id": trade_id, "pair": pair}

    try:
        api_request(port, user, password, "POST", f"/trades/{trade_id}/reload")
        out["reload"] = "ok"
    except Exception as exc:
        out["reload"] = str(exc)

    try:
        api_request(
            port,
            user,
            password,
            "POST",
            "/forceexit",
            {"tradeid": trade_id, "ordertype": "market"},
        )
        out["action"] = "forceexit"
        out["ok"] = True
        return out
    except Exception as exc:
        out["forceexit"] = str(exc)

    # Position already flat on exchange — archive into history instead of DELETE.
    base = base or DEFAULT_BASE
    db = db_path_for_bot(base, bot)
    if not db:
        out["ok"] = False
        out["error"] = "db not found"
        return out

    symbol = ft_pair_to_bybit(pair)
    close_rate = None
    profit_abs = None
    if key and secret:
        row = bybit_closed_pnl_near(key, secret, symbol)
        if row:
            try:
                if row.get("avgExitPrice"):
                    close_rate = float(row["avgExitPrice"])
                if row.get("closedPnl") is not None:
                    profit_abs = float(row["closedPnl"])
            except (TypeError, ValueError):
                pass
        if close_rate is None:
            close_rate = bybit_last_price(key, secret, symbol)

    archived = archive_trade_in_db(
        db,
        trade_id,
        close_rate=close_rate,
        profit_abs=profit_abs,
        exit_reason="reconcile",
    )
    out.update(archived)
    # Refresh bot process so ORM doesn't overwrite the archived row with a stale open trade.
    try:
        api_request(port, user, password, "POST", "/reload_config")
        out["bot_reload"] = "ok"
    except Exception as exc:
        out["bot_reload"] = str(exc)
        out.setdefault(
            "warning",
            "Trade archived in DB; bot reload failed — restart the bot if it stays open in UI",
        )
    return out


def cancel_orphan_stop_orders(
    base: Path,
    key: str,
    secret: str,
    ghosts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cancel untriggered stop orders for ghost trades."""
    results: list[dict[str, Any]] = []
    if not ghosts:
        return results
    ghost_ids = {(g["bot"], g["trade_id"]) for g in ghosts}
    for bot, meta in BOTS.items():
        db = db_path_for_config(base, meta["config"])
        if not db.exists():
            continue
        conn = sqlite3.connect(db)
        for trade_id, pair in conn.execute(
            "SELECT id, pair FROM trades WHERE is_open = 1"
        ):
            if (bot, trade_id) not in ghost_ids:
                continue
            symbol = ft_pair_to_bybit(pair)
            for order_id, status in conn.execute(
                """
                SELECT order_id, status FROM orders
                WHERE ft_trade_id = ? AND ft_order_side = 'stoploss' AND status = 'open'
                """,
                (trade_id,),
            ):
                if not order_id:
                    continue
                try:
                    resp = bybit_cancel_order(key, secret, symbol, order_id)
                    ok = resp.get("retCode") == 0
                    results.append(
                        {
                            "bot": bot,
                            "trade_id": trade_id,
                            "symbol": symbol,
                            "order_id": order_id,
                            "ok": ok,
                            "response": resp.get("retMsg") if not ok else "cancelled",
                        }
                    )
                except Exception as exc:
                    results.append(
                        {
                            "bot": bot,
                            "trade_id": trade_id,
                            "symbol": symbol,
                            "order_id": order_id,
                            "ok": False,
                            "response": str(exc),
                        }
                    )
        conn.close()
    return results


def fix_reconcile(
    base: Path | None = None,
    env_path: Path | None = None,
) -> dict[str, Any]:
    base = base or DEFAULT_BASE
    env_path = env_path or DEFAULT_ENV
    env = load_env(env_path)
    user = env.get("FREQUI_USERNAME", "cryptotools")
    password = env.get("FREQUI_PASSWORD", "cryptotools")
    key = env.get("BYBIT_API_KEY", "")
    secret = env.get("BYBIT_API_SECRET", "")

    before = reconcile(base, env_path)
    ghosts = before.get("ghosts") or []
    cancelled = cancel_orphan_stop_orders(base, key, secret, ghosts) if key and secret else []
    fixed: list[dict[str, Any]] = []
    for ghost in ghosts:
        fixed.append(
            fix_ghost_trade(
                ghost,
                user,
                password,
                base=base,
                key=key,
                secret=secret,
            )
        )

    after = reconcile(base, env_path)
    return {
        "before": before,
        "after": after,
        "cancelled_orders": cancelled,
        "fixed": fixed,
        "ok": after.get("ghost_count", 0) == 0,
    }


def archive_stale_trade(
    bot: str,
    trade_id: int,
    base: Path | None = None,
    env_path: Path | None = None,
) -> dict[str, Any]:
    """UI helper: archive a stale open trade into closed history (no DELETE)."""
    base = base or DEFAULT_BASE
    env = load_env(env_path or DEFAULT_ENV)
    if bot not in BOTS:
        raise ValueError("invalid bot")
    ghost = {
        "bot": bot,
        "trade_id": int(trade_id),
        "pair": "",
        "port": BOTS[bot]["port"],
        "side": "",
    }
    db = db_path_for_bot(base, bot)
    if not db:
        raise ValueError("db not found")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT pair, is_short FROM trades WHERE id = ?", (int(trade_id),)
    ).fetchone()
    conn.close()
    if not row:
        raise ValueError("trade not found")
    ghost["pair"] = row["pair"]
    ghost["side"] = ft_side_key(row["is_short"])
    return fix_ghost_trade(
        ghost,
        env.get("FREQUI_USERNAME", "cryptotools"),
        env.get("FREQUI_PASSWORD", "cryptotools"),
        base=base,
        key=env.get("BYBIT_API_KEY", ""),
        secret=env.get("BYBIT_API_SECRET", ""),
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Reconcile FT open trades vs Bybit")
    ap.add_argument("--fix", action="store_true", help="Remove ghost trades from bot DB")
    ap.add_argument("--json", action="store_true", help="Print JSON")
    args = ap.parse_args()

    result = fix_reconcile() if args.fix else reconcile()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if args.fix:
            print(
                f"Fixed {sum(1 for x in result.get('fixed', []) if x.get('ok'))}/"
                f"{len(result.get('fixed', []))} ghosts; "
                f"after: FT={result['after']['ft_open_count']} "
                f"Bybit={result['after']['bybit_position_count']}"
            )
        else:
            print(
                f"FT open={result['ft_open_count']} Bybit={result['bybit_position_count']} "
                f"ghosts={result['ghost_count']} exchange_only={result['exchange_only_count']}"
            )
            for g in result.get("ghosts", []):
                print(f"  ghost [{g['bot']}] #{g['trade_id']} {g['pair']} {g['side']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

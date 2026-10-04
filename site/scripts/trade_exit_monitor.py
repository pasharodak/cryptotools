#!/usr/bin/env python3
"""Monitor and close open strategy trades for shared-bot users."""
from __future__ import annotations

import importlib
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

log = logging.getLogger("trade_exit_monitor")

HEDGE_SUFFIX = ":hedge"
INV_SUFFIX = ":inv"


def base_enter_tag(tag: str | None) -> str:
    if not tag:
        return ""
    t = str(tag)
    for suffix in (HEDGE_SUFFIX, INV_SUFFIX):
        while t.endswith(suffix):
            t = t[: -len(suffix)]
    return t


def is_test_tag(tag: str) -> bool:
    return tag.endswith("TestStrategy")


@dataclass
class TradeRow:
    id: int
    pair: str
    is_short: bool
    amount: float
    open_rate: float
    stake_amount: float
    enter_tag: str
    strategy: str
    leverage: float


def load_open_trades(db_path) -> list[TradeRow]:
    if not db_path.is_file():
        return []
    out: list[TradeRow] = []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            for r in conn.execute(
                """
                SELECT id, pair, is_short, amount, open_rate, stake_amount,
                       enter_tag, strategy, leverage
                FROM trades WHERE is_open = 1 ORDER BY id
                """
            ):
                out.append(
                    TradeRow(
                        id=int(r["id"]),
                        pair=str(r["pair"]),
                        is_short=bool(r["is_short"]),
                        amount=float(r["amount"] or 0),
                        open_rate=float(r["open_rate"] or 0),
                        stake_amount=float(r["stake_amount"] or 0),
                        enter_tag=str(r["enter_tag"] or ""),
                        strategy=str(r["strategy"] or ""),
                        leverage=float(r["leverage"] or 1),
                    )
                )
    except sqlite3.Error as exc:
        log.warning("load open trades %s: %s", db_path, exc)
    return out


def profit_ratio(trade: TradeRow, rate: float) -> float:
    if not trade.open_rate or not rate:
        return 0.0
    lev = max(trade.leverage or 1.0, 1.0)
    if trade.is_short:
        return ((trade.open_rate - rate) / trade.open_rate) * lev
    return ((rate - trade.open_rate) / trade.open_rate) * lev


def update_mark_pnl(db_path, trade_id: int, ratio: float, abs_pnl: float) -> None:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE trades SET close_profit = ?, close_profit_abs = ?
                WHERE id = ? AND is_open = 1
                """,
                (ratio, abs_pnl, trade_id),
            )
            conn.commit()
    except sqlite3.Error:
        pass


def risk_thresholds(user_id: str, tag: str, *, tenant_dir, load_json, load_user_config) -> tuple[float, float]:
    cfg = load_user_config(user_id)
    sl = float(cfg.get("stoploss") or -0.15)
    roi = cfg.get("minimal_roi") or {"0": 0.05}
    tp = float(roi.get("0") or roi.get(0) or 0.05)
    if is_test_tag(tag):
        ts = load_json(tenant_dir(user_id) / "test_strategy_settings.json", {})
        if isinstance(ts, dict):
            if ts.get("stoploss") is not None:
                sl = -abs(float(ts["stoploss"]))
            if ts.get("take_profit") is not None:
                tp = abs(float(ts["take_profit"]))
    return sl, tp


def klines_to_df(rows: list[list[Any]]) -> pd.DataFrame | None:
    if not rows:
        return None
    df = pd.DataFrame(
        rows,
        columns=["date_ms", "open", "high", "low", "close", "volume", "turnover"],
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date_ms"].astype(int), unit="ms", utc=True)
    return df


def try_strategy_exit(tag: str, df: pd.DataFrame, trade: TradeRow, rate: float) -> str | None:
    strategies_dir = Path(__file__).resolve().parent.parent / "user_data" / "strategies"
    if str(strategies_dir) not in sys.path:
        sys.path.insert(0, str(strategies_dir))
    try:
        mod = importlib.import_module(tag)
        cls = getattr(mod, tag, None)
        if cls is None:
            return None
        inst = cls({})
        fn = getattr(inst, "exit_reason_from_ohlcv", None)
        if not callable(fn):
            return None
        stub = SimpleNamespace(
            pair=trade.pair,
            is_short=trade.is_short,
            open_rate=trade.open_rate,
            enter_tag=trade.enter_tag,
        )
        ind = getattr(inst, "populate_indicators", None)
        work = df
        if callable(ind):
            work = ind(df.copy(), {"pair": trade.pair})
        reason = fn(work, stub, rate)
        return str(reason) if reason else None
    except Exception as exc:
        log.debug("custom exit %s: %s", tag, exc)
        return None


def monitor_user_exits(user_id: str, ctx: dict[str, Any]) -> None:
    get_exchange = ctx["get_exchange"]
    db_path_fn = ctx["db_path"]
    tenant_dir = ctx["tenant_dir"]
    load_json = ctx["load_json"]
    load_user_config = ctx["load_user_config"]
    archive_trade_in_db = ctx["archive_trade_in_db"]
    ft_pair_to_symbol = ctx["ft_pair_to_symbol"]
    fetch_public_klines = ctx["fetch_public_klines"]

    path = db_path_fn(user_id)
    trades = load_open_trades(path)
    if not trades:
        return
    ex = get_exchange(user_id)
    if ex is None:
        return
    try:
        pos_map = ex.positions_map()
    except Exception as exc:
        log.warning("positions user=%s: %s", user_id, exc)
        return

    for trade in trades:
        sym = ft_pair_to_symbol(trade.pair)
        side_key = "sell" if trade.is_short else "buy"
        pos = pos_map.get((sym, side_key))
        try:
            mark = ex.last_price(sym)
        except Exception:
            mark = trade.open_rate
        ratio = profit_ratio(trade, mark)
        abs_pnl = ratio * (trade.stake_amount or 0)
        update_mark_pnl(path, trade.id, ratio, abs_pnl)

        if not pos or float(pos.get("size") or 0) <= 0:
            archive_trade_in_db(
                path,
                trade.id,
                close_rate=mark,
                profit_abs=abs_pnl,
                exit_reason="exchange_flat",
            )
            log.info("ARCHIVE user=%s #%s %s (flat on exchange)", user_id, trade.id, trade.pair)
            try:
                import telegram_links as tg_links

                tg_links.notify_user(
                    user_id,
                    tg_links.format_exit_ru(
                        pair=trade.pair,
                        strategy=base_enter_tag(trade.enter_tag or trade.strategy),
                        reason="exchange_flat",
                        pnl_pct=ratio * 100,
                        pnl_usdt=abs_pnl,
                        stake=float(trade.stake_amount or 0) or None,
                        is_short=bool(trade.is_short),
                        close_rate=mark,
                    ),
                )
            except Exception:
                pass
            continue

        tag = base_enter_tag(trade.enter_tag or trade.strategy)
        sl, tp = risk_thresholds(
            user_id, tag, tenant_dir=tenant_dir, load_json=load_json, load_user_config=load_user_config
        )
        exit_reason: str | None = None
        if tp > 0 and ratio >= tp:
            exit_reason = "roi_take_profit"
        elif sl < 0 and ratio <= sl:
            exit_reason = "stop_loss"

        if not exit_reason:
            try:
                rows = fetch_public_klines(sym, interval="5", limit=250)
                df = klines_to_df(rows)
                if df is not None and not df.empty:
                    custom = try_strategy_exit(tag, df, trade, mark)
                    if custom:
                        exit_reason = str(custom)
            except Exception as exc:
                log.debug("klines exit user=%s %s: %s", user_id, trade.pair, exc)

        if not exit_reason:
            continue

        try:
            res = ex.market_close(pair=trade.pair, is_short=trade.is_short)
            close_rate = float(res.get("price") or mark)
            if res.get("closed"):
                final_ratio = profit_ratio(trade, close_rate)
                archive_trade_in_db(
                    path,
                    trade.id,
                    close_rate=close_rate,
                    profit_abs=final_ratio * (trade.stake_amount or 0),
                    exit_reason=exit_reason,
                )
                log.info(
                    "EXIT user=%s #%s %s reason=%s pnl=%.2f%%",
                    user_id,
                    trade.id,
                    trade.pair,
                    exit_reason,
                    final_ratio * 100,
                )
                try:
                    import telegram_links as tg_links

                    pnl_abs = final_ratio * (trade.stake_amount or 0)
                    tg_links.notify_user(
                        user_id,
                        tg_links.format_exit_ru(
                            pair=trade.pair,
                            strategy=tag,
                            reason=exit_reason,
                            pnl_pct=final_ratio * 100,
                            pnl_usdt=pnl_abs,
                            stake=float(trade.stake_amount or 0) or None,
                            is_short=bool(trade.is_short),
                            close_rate=close_rate,
                        ),
                    )
                except Exception:
                    pass
        except Exception as exc:
            log.error("close failed user=%s #%s: %s", user_id, trade.id, exc)


def monitor_all_users(ctx: dict[str, Any]) -> None:
    import tenant_manager as tm

    users: set[str] = set()
    for user in tm.load_users():
        uid = str(user.get("id") or "")
        if not uid or not user.get("enabled", True):
            continue
        if ctx["count_open_trades"](uid) > 0:
            users.add(uid)
    for uid in sorted(users):
        try:
            monitor_user_exits(uid, ctx)
        except Exception as exc:
            log.exception("exit monitor user=%s: %s", uid, exc)

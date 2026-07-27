"""Align backtest trades with historical scanner arm times for player replay."""
from __future__ import annotations

from typing import Any

from .scan_replay import align_scan_start


def resolve_armed_at(
    scan_arm_ms: int | None,
    trades: list[dict],
    range_start_ms: int,
    *,
    allow_trade_fallback: bool = True,
) -> int | None:
    """Use scan arm time, or fall back to scan boundary before first trade."""
    if scan_arm_ms is not None:
        return scan_arm_ms
    if not allow_trade_fallback or not trades:
        return None
    first_open = min(t["open_ms"] for t in trades)
    arm = align_scan_start(first_open)
    return max(range_start_ms, arm)


def filter_trades_after_arm(trades: list[dict], armed_at_ms: int | None) -> list[dict]:
    if armed_at_ms is None:
        return []
    return [t for t in trades if t["open_ms"] >= armed_at_ms]


def whitelist_at_ms(timeline: list[tuple[int, frozenset[str] | set[str]]], ms: int) -> set[str]:
    active: set[str] = set()
    for ts, pairs in timeline:
        if ts > ms:
            break
        active = set(pairs)
    return active


def active_pairs_at_ms(
    timeline: list[tuple[int, frozenset[str] | set[str]]],
    ms: int,
    *,
    grace_scans: int = 2,
) -> set[str]:
    """Pairs in current whitelist plus recent scans (prod-like hold window)."""
    recent: list[set[str]] = []
    for ts, pairs in timeline:
        if ts > ms:
            break
        recent.append(set(pairs))
        if len(recent) > grace_scans:
            recent.pop(0)
    out: set[str] = set()
    for s in recent:
        out |= s
    return out


def filter_trades_by_pair_pnl_history(
    trades: list[dict],
    *,
    window_ms: int = 7 * 24 * 3600 * 1000,
    min_closed: int = 2,
    min_cum_loss_usdt: float = 0.0,
) -> list[dict]:
    """Skip opens after consecutive losses (and optional cumulative loss) in lookback."""
    if not trades or min_closed <= 0:
        return list(trades)
    ordered = sorted(trades, key=lambda t: t["open_ms"])
    closed_by_pair: dict[str, list[dict]] = {}
    out: list[dict] = []
    for t in ordered:
        pair = t["pair"]
        open_ms = t["open_ms"]
        recent = sorted(
            [
                c
                for c in closed_by_pair.get(pair, [])
                if open_ms - window_ms <= c["close_ms"] < open_ms
            ],
            key=lambda c: c["close_ms"],
        )
        tail = recent[-min_closed:]
        consec_losses = (
            len(tail) >= min_closed
            and all(float(c.get("profit_abs") or 0) < 0 for c in tail)
        )
        cum_loss = sum(float(c.get("profit_abs") or 0) for c in recent)
        if consec_losses and (min_cum_loss_usdt <= 0 or cum_loss <= -min_cum_loss_usdt):
            continue
        out.append(t)
        closed_by_pair.setdefault(pair, []).append(t)
    return out


def filter_trades_rolling_whitelist(
    trades: list[dict],
    timeline: list[tuple[int, frozenset[str] | set[str]]],
    *,
    grace_scans: int = 2,
) -> list[dict]:
    if not timeline:
        return []
    return [
        t
        for t in trades
        if t.get("pair") in active_pairs_at_ms(timeline, t["open_ms"], grace_scans=grace_scans)
    ]


def pair_ever_whitelisted(pair: str, timeline: list[tuple[int, frozenset[str] | set[str]]]) -> bool:
    return any(pair in pairs for _, pairs in timeline)


def summarize_trades(trades: list[dict]) -> dict[str, Any]:
    profit = sum(t["profit_abs"] for t in trades)
    wins = sum(1 for t in trades if t["profit_abs"] > 0)
    return {
        "total_trades": len(trades),
        "profit_abs": round(profit, 4),
        "wins": wins,
        "losses": len(trades) - wins,
    }

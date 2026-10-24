"""DeepSeek book bot: one $50 margin budget, model decides entries and exits."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import llm_settings

TAG = "Deepseek50"
BOT = "llm50"
BUDGET_USDT = 50.0
LEVERAGE = 3.0
MIN_STAKE = 5.0
# 25 liquid Bybit USDT perps. The first 12 match the other DeepSeek bot;
# the rest are the next liquid crypto names by turnover on 2026-10-17,
# without stocks, metals, and one-day tickers.
PAIRS: tuple[str, ...] = (
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
    "DOGE/USDT:USDT",
    "ADA/USDT:USDT",
    "AVAX/USDT:USDT",
    "LINK/USDT:USDT",
    "LTC/USDT:USDT",
    "DOT/USDT:USDT",
    "TON/USDT:USDT",
    "NEAR/USDT:USDT",
    "ZEC/USDT:USDT",
    "HYPE/USDT:USDT",
    "TIA/USDT:USDT",
    "WLD/USDT:USDT",
    "INJ/USDT:USDT",
    "ENA/USDT:USDT",
    "SUI/USDT:USDT",
    "UNI/USDT:USDT",
    "1000PEPE/USDT:USDT",
    "ARB/USDT:USDT",
    "TAO/USDT:USDT",
    "AAVE/USDT:USDT",
)
# Stake-PnL ratios used when the model omits a price. At 3x that is ~2% / 3% of price.
DEFAULT_STOP = -0.06
DEFAULT_TAKE = 0.09
STATUS_NAME = "llm_book_status.json"


def pairs() -> list[str]:
    return list(PAIRS)


def trades_db_for(user_id: str) -> Path:
    import tenant_manager as tm

    root = tm.BASE / "user_data" if str(user_id) == "admin" else tm.tenant_user_data(str(user_id))
    preferred = root / "tradesv3-strategy.sqlite"
    if preferred.is_file():
        return preferred
    found = sorted(root.glob("tradesv3*.sqlite"))
    return found[0] if found else preferred


def _is_book(enter_tag: Any, strategy: Any) -> bool:
    tag = str(enter_tag or "")
    strat = str(strategy or "")
    return tag.startswith(TAG) or strat == TAG


def open_positions(db_path: Path) -> list[dict[str, Any]]:
    return _open_rows(db_path)


def _open_rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    query = """
        SELECT id, pair, strategy, enter_tag, is_short, open_rate, stake_amount, leverage, stop_loss
        FROM trades
        WHERE is_open = 1
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not _is_book(row["enter_tag"], row["strategy"]):
            continue
        stake = float(row["stake_amount"] or 0)
        out.append(
            {
                "trade_id": int(row["id"]),
                "pair": str(row["pair"] or ""),
                "side": "short" if row["is_short"] else "long",
                "entry": float(row["open_rate"] or 0),
                "stake_usdt": round(stake, 2),
                "leverage": float(row["leverage"] or LEVERAGE),
                "stop_loss": row["stop_loss"],
            }
        )
    return out


def used_margin(positions: list[dict[str, Any]]) -> float:
    return round(sum(float(row.get("stake_usdt") or 0) for row in positions), 2)


def free_margin(positions: list[dict[str, Any]]) -> float:
    return round(max(0.0, BUDGET_USDT - used_margin(positions)), 2)


def build_messages(
    market: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    *,
    free: float,
    used: float,
) -> list[dict[str, str]]:
    system = (
        "You manage one Bybit USDT-perpetual book. Total margin budget is "
        f"{BUDGET_USDT:.0f} USDT and leverage is fixed at {LEVERAGE:.0f}x. "
        "You cannot change leverage or spend more than the free margin. "
        "Decide what to do with the book: open a long (BUY), open a short (SELL), "
        "keep a position (HOLD), or close it (CLOSE). "
        "Size new positions from free margin. Every new position needs a stop_loss "
        "and a take_profit as absolute prices. Prefer few positions over churn. "
        "The market list is long. Return at most 6 actions, only for pairs you open, "
        "close, or must comment on. Omit every other pair. "
        "Do not invent order book, funding, or open interest. "
        "Return only JSON, no text outside it."
    )
    user = {
        "budget_usdt": BUDGET_USDT,
        "used_margin_usdt": used,
        "free_margin_usdt": free,
        "leverage": LEVERAGE,
        "open_positions": positions,
        "market": market,
        "response": {
            "actions": [
                {
                    "pair": "BTC/USDT:USDT",
                    "action": "BUY",
                    "stake_usdt": 10,
                    "stop_loss": 0,
                    "take_profit": 0,
                    "confidence": 0.7,
                    "reasoning": "short reason",
                }
            ]
        },
    }
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                "Account and market state:\n"
                + json.dumps(user, ensure_ascii=False)
                + "\n\naction is BUY, SELL, HOLD, or CLOSE. "
                "BUY and SELL open a new position only. CLOSE closes that pair. "
                "stake_usdt is margin, not notional, and must be <= free_margin_usdt."
            ),
        },
    ]


def _extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("model response is not a JSON object")
    return data


def parse_actions(content: str) -> list[dict[str, Any]]:
    try:
        data = _extract_json(content)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    rows = data.get("actions")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        if data.get("action"):
            rows = [data]
        else:
            return []
    allowed = {llm_settings.normalize_pair(pair) for pair in pairs()}
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "HOLD").strip().upper()
        if action not in {"BUY", "SELL", "HOLD", "CLOSE"}:
            action = "HOLD"
        try:
            pair = llm_settings.normalize_pair(str(row.get("pair") or ""))
        except ValueError:
            continue
        if pair not in allowed:
            continue
        try:
            stake = float(row.get("stake_usdt") or 0)
        except (TypeError, ValueError):
            stake = 0.0
        try:
            confidence = float(row.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(1.0, max(0.0, confidence))

        def _price(key: str) -> float | None:
            try:
                value = float(row.get(key))
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None

        out.append(
            {
                "pair": pair,
                "action": action,
                "stake_usdt": stake,
                "stop_loss": _price("stop_loss"),
                "take_profit": _price("take_profit"),
                "confidence": confidence,
                "reasoning": " ".join(str(row.get("reasoning") or "").split())[:400],
            }
        )
    return out


def stake_for(action: dict[str, Any], free: float) -> float:
    """Margin to commit. Missing size takes a small slice of what is left."""
    free = max(0.0, float(free))
    if free < MIN_STAKE:
        return 0.0
    raw = float(action.get("stake_usdt") or 0)
    if raw <= 0:
        raw = min(10.0, free)
    elif raw <= 1:
        raw = raw * free
    return round(min(free, max(MIN_STAKE, raw)), 2)


def ratios_for(action: dict[str, Any], price: float, side: str) -> tuple[float, float] | None:
    """Stake-PnL ratios (stop < 0, take > 0) from absolute prices, else defaults."""
    if price <= 0:
        return None
    sl_price = action.get("stop_loss")
    tp_price = action.get("take_profit")
    is_short = side == "short"
    if sl_price and tp_price:
        if is_short:
            if float(sl_price) <= price or float(tp_price) >= price:
                return None
            sl_move = (float(sl_price) - price) / price
            tp_move = (price - float(tp_price)) / price
        else:
            if float(sl_price) >= price or float(tp_price) <= price:
                return None
            sl_move = (price - float(sl_price)) / price
            tp_move = (float(tp_price) - price) / price
        sl_ratio = -abs(sl_move) * LEVERAGE
        tp_ratio = abs(tp_move) * LEVERAGE
    else:
        sl_ratio = DEFAULT_STOP
        tp_ratio = DEFAULT_TAKE
    sl_ratio = max(-0.9, float(sl_ratio))
    tp_ratio = max(0.01, float(tp_ratio))
    return sl_ratio, tp_ratio


def stats(db_path: Path) -> dict[str, Any]:
    empty = {
        "budget_usdt": BUDGET_USDT,
        "used_usdt": 0.0,
        "free_usdt": BUDGET_USDT,
        "open_count": 0,
        "closed_count": 0,
        "wins": 0,
        "losses": 0,
        "realized_usdt": 0.0,
    }
    if not db_path.is_file():
        return empty
    query = """
        SELECT is_open, enter_tag, strategy, close_profit_abs, stake_amount,
               amount, open_rate, close_rate, leverage
        FROM trades
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query).fetchall()
    except sqlite3.Error:
        return empty
    used = 0.0
    open_count = 0
    closed = wins = losses = 0
    realized = 0.0
    for row in rows:
        if not _is_book(row["enter_tag"], row["strategy"]):
            continue
        if int(row["is_open"] or 0) == 1:
            open_count += 1
            used += float(row["stake_amount"] or 0)
            continue
        closed += 1
        pnl = _net_pnl(row)
        realized += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
    used = round(used, 2)
    return {
        "budget_usdt": BUDGET_USDT,
        "used_usdt": used,
        "free_usdt": round(max(0.0, BUDGET_USDT - used), 2),
        "open_count": open_count,
        "closed_count": closed,
        "wins": wins,
        "losses": losses,
        "realized_usdt": round(realized, 4),
    }


def _net_pnl(row: sqlite3.Row) -> float:
    try:
        gross = float(row["close_profit_abs"] or 0)
    except (TypeError, ValueError):
        gross = 0.0
    try:
        amount = float(row["amount"] or 0)
        open_rate = float(row["open_rate"] or 0)
        close_rate = float(row["close_rate"] or 0)
    except (TypeError, ValueError):
        amount, open_rate, close_rate = 0.0, 0.0, 0.0
    if amount > 0 and open_rate > 0:
        close_rate = close_rate if close_rate > 0 else open_rate
        fees = amount * open_rate * 0.001 + amount * close_rate * 0.001
        return gross - fees
    try:
        stake = float(row["stake_amount"] or 0)
        lev = float(row["leverage"] or 1) or 1.0
    except (TypeError, ValueError):
        return gross
    return gross - stake * lev * 0.002


def write_status(base: Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = base / "user_data" / STATUS_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def read_status(base: Path) -> dict[str, Any]:
    path = base / "user_data" / STATUS_NAME
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}

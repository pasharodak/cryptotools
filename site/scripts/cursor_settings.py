"""Per-user settings for the standalone Cursor bot."""
from __future__ import annotations

import os
import re
from typing import Any

import control_plane as cp

KEY = "cursor_bot"
TAG = "CursorLlmStrategy"
# Liquid Bybit USDT perps. One model call per pair each cycle.
DEFAULT_PAIRS: tuple[str, ...] = (
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
)
DEFAULT_TAKE_PROFIT = 0.03
DEFAULT_STOPLOSS = -0.02
DEFAULT_MAX_OPEN = 1
DEFAULT_STAKE = 5.0
DEFAULT_MIN_CONFIDENCE = 0.65
TP_MIN = 0.005
TP_MAX = 0.20
MAX_OPEN_LIMIT = 10
STAKE_MIN = 1.0
STAKE_MAX = 100.0
CONF_MIN = 0.50
CONF_MAX = 0.95
MAX_PAIRS = 30
_BASE_RE = re.compile(r"^[A-Z0-9]{2,20}$")


def default_pairs() -> list[str]:
    """Built-in list. CT_CURSOR_PAIRS replaces it only when the user has not saved a list."""
    raw = os.environ.get("CT_CURSOR_PAIRS")
    if raw is not None and str(raw).strip():
        out = [part.strip() for part in str(raw).split(",") if part.strip()]
        if out:
            return out
    return list(DEFAULT_PAIRS)


def pairs() -> list[str]:
    return default_pairs()


def normalize_pair(raw: str) -> str:
    text = str(raw or "").strip().upper().replace(" ", "").replace("-", "/")
    if not text:
        raise ValueError("пустая пара")
    if ":" in text:
        text = text.split(":", 1)[0]
    if "/" in text:
        base, quote = text.split("/", 1)
        if quote != "USDT":
            raise ValueError(f"только пары USDT, получено {raw}")
    elif text.endswith("USDT") and len(text) > 4:
        base = text[:-4]
    else:
        base = text
    if not _BASE_RE.fullmatch(base):
        raise ValueError(f"непонятная пара: {raw}")
    return f"{base}/USDT:USDT"


def normalize_pairs(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [part for part in re.split(r"[,;\s]+", value) if part.strip()]
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError("pairs must be a list")
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        pair = normalize_pair(str(item))
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    if len(out) > MAX_PAIRS:
        raise ValueError(f"не больше {MAX_PAIRS} пар")
    return out


def _clamp_stake(value: float) -> float:
    return round(min(STAKE_MAX, max(STAKE_MIN, float(value))), 2)


def _clamp_tp(value: float) -> float:
    return min(TP_MAX, max(TP_MIN, float(value)))


def _clamp_max(value: int) -> int:
    return min(MAX_OPEN_LIMIT, max(0, int(value)))


def _clamp_conf(value: float) -> float:
    return round(min(CONF_MAX, max(CONF_MIN, float(value))), 4)


def _confidence_from(data: dict[str, Any]) -> float:
    if "min_confidence_pct" in data and data["min_confidence_pct"] is not None:
        return _clamp_conf(float(data["min_confidence_pct"]) / 100.0)
    if "min_confidence" in data and data["min_confidence"] is not None:
        return _clamp_conf(float(data["min_confidence"]))
    return DEFAULT_MIN_CONFIDENCE


def normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    if "take_profit" in data:
        tp = float(data["take_profit"])
    elif "take_profit_pct" in data:
        tp = float(data["take_profit_pct"]) / 100.0
    else:
        tp = DEFAULT_TAKE_PROFIT
    if "max_open_trades" in data and data["max_open_trades"] is not None:
        max_open = int(data["max_open_trades"])
    else:
        max_open = DEFAULT_MAX_OPEN
    if "stake_amount" in data and data["stake_amount"] is not None:
        stake = float(data["stake_amount"])
    else:
        stake = DEFAULT_STAKE
    if "pairs" in data and data["pairs"] is not None:
        pair_list = normalize_pairs(data["pairs"])
    else:
        pair_list = default_pairs()
    sl = float(data.get("stoploss") if data.get("stoploss") is not None else DEFAULT_STOPLOSS)
    if sl > 0:
        sl = -abs(sl)
    return {
        "take_profit": _clamp_tp(tp),
        "stoploss": sl,
        "max_open_trades": _clamp_max(max_open),
        "stake_amount": _clamp_stake(stake),
        "min_confidence": _confidence_from(data),
        "pairs": pair_list,
    }


def payload(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    data = normalize(settings)
    tp = float(data["take_profit"])
    return {
        "take_profit": tp,
        "take_profit_pct": round(tp * 100, 2),
        "stoploss": float(data["stoploss"]),
        "max_open_trades": int(data["max_open_trades"]),
        "stake_amount": float(data["stake_amount"]),
        "min_confidence": float(data["min_confidence"]),
        "min_confidence_pct": round(float(data["min_confidence"]) * 100, 1),
        "pairs": list(data["pairs"]),
    }


def load(user_id: str) -> dict[str, Any]:
    raw = cp.get_user_setting(str(user_id), KEY, None)
    return payload(raw if isinstance(raw, dict) else None)


def save(user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    current = load(user_id)
    patch = patch if isinstance(patch, dict) else {}
    merged: dict[str, Any] = {
        "stoploss": current["stoploss"],
        "max_open_trades": current["max_open_trades"],
        "stake_amount": current["stake_amount"],
        "pairs": list(current["pairs"]),
    }
    if "min_confidence_pct" in patch and "min_confidence" not in patch:
        merged["min_confidence_pct"] = patch["min_confidence_pct"]
    else:
        merged["min_confidence"] = patch.get("min_confidence", current["min_confidence"])
    # take_profit in the stored payload is a ratio. A percent patch must not
    # be ignored just because the current ratio is also present.
    if "take_profit_pct" in patch and "take_profit" not in patch:
        merged["take_profit_pct"] = patch["take_profit_pct"]
    else:
        merged["take_profit"] = patch.get("take_profit", current["take_profit"])
    if "stoploss" in patch:
        merged["stoploss"] = patch["stoploss"]
    if "max_open_trades" in patch:
        merged["max_open_trades"] = patch["max_open_trades"]
    if "stake_amount" in patch:
        merged["stake_amount"] = patch["stake_amount"]
    if "pairs" in patch:
        merged["pairs"] = patch["pairs"]
    stored = normalize(merged)
    cp.set_user_setting(str(user_id), KEY, stored)
    return payload(stored)


def pairs_for(user_id: str) -> list[str]:
    return list(load(user_id)["pairs"])


def publish_floor(user_ids: list[str]) -> float:
    """Lowest per-user threshold. The bot publishes a signal if any trader can take it."""
    if not user_ids:
        return DEFAULT_MIN_CONFIDENCE
    return min(float(load(user_id)["min_confidence"]) for user_id in user_ids)


def signal_confidence(ev: dict[str, Any]) -> float | None:
    ml = ev.get("ml") if isinstance(ev.get("ml"), dict) else {}
    raw = ml.get("confidence_profit")
    if raw is None:
        indicators = ev.get("indicators") if isinstance(ev.get("indicators"), dict) else {}
        raw = indicators.get("llm_conf")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 1:
        value = value / 100.0
    return value

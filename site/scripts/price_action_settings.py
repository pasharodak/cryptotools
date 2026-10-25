"""Per-user settings for the standalone Price Action bot."""
from __future__ import annotations

import re
from typing import Any

import control_plane as cp

KEY = "price_action_bot"
TAGS: tuple[str, ...] = ("PaTrendPullback", "PaLiquiditySweep")
SETUP_FLAGS = {
    "trend_pullback": "enable_trend_pullback",
    "liquidity_sweep": "enable_liquidity_sweep",
}
SETUP_LABELS = {
    "trend_pullback": "Trend Pullback",
    "liquidity_sweep": "Liquidity Sweep",
}
TAG_TO_SETUP = {
    "PaTrendPullback": "trend_pullback",
    "PaLiquiditySweep": "liquidity_sweep",
}
DEFAULT_ENABLE_TREND_PULLBACK = True
DEFAULT_ENABLE_LIQUIDITY_SWEEP = True
# Shared Strategy whitelist minus delisted Bybit pairs (BLAST, SCRT, STG, VANRY).
DEFAULT_PAIRS: tuple[str, ...] = (
    "1000BONK/USDT:USDT",
    "1000FLOKI/USDT:USDT",
    "1000NEIROCTO/USDT:USDT",
    "1000PEPE/USDT:USDT",
    "ADA/USDT:USDT",
    "AGLD/USDT:USDT",
    "AIXBT/USDT:USDT",
    "ALGO/USDT:USDT",
    "ALICE/USDT:USDT",
    "APE/USDT:USDT",
    "APT/USDT:USDT",
    "ARB/USDT:USDT",
    "ARPA/USDT:USDT",
    "AVAX/USDT:USDT",
    "AXS/USDT:USDT",
    "B/USDT:USDT",
    "BCH/USDT:USDT",
    "BE/USDT:USDT",
    "BIO/USDT:USDT",
    "BLUR/USDT:USDT",
    "BNB/USDT:USDT",
    "CHZ/USDT:USDT",
    "CRV/USDT:USDT",
    "DEEP/USDT:USDT",
    "DEXE/USDT:USDT",
    "DOGE/USDT:USDT",
    "DOT/USDT:USDT",
    "DYDX/USDT:USDT",
    "EIGEN/USDT:USDT",
    "ENA/USDT:USDT",
    "ENS/USDT:USDT",
    "ETC/USDT:USDT",
    "ETH/USDT:USDT",
    "ETHFI/USDT:USDT",
    "FIL/USDT:USDT",
    "GALA/USDT:USDT",
    "GMT/USDT:USDT",
    "GOAT/USDT:USDT",
    "HBAR/USDT:USDT",
    "HEI/USDT:USDT",
    "HMSTR/USDT:USDT",
    "HYPE/USDT:USDT",
    "ICP/USDT:USDT",
    "ID/USDT:USDT",
    "IMX/USDT:USDT",
    "INJ/USDT:USDT",
    "JTO/USDT:USDT",
    "KAS/USDT:USDT",
    "LDO/USDT:USDT",
    "LINK/USDT:USDT",
    "LTC/USDT:USDT",
    "M/USDT:USDT",
    "MANA/USDT:USDT",
    "ME/USDT:USDT",
    "MINA/USDT:USDT",
    "MOODENG/USDT:USDT",
    "MORPHO/USDT:USDT",
    "MOVR/USDT:USDT",
    "MSTR/USDT:USDT",
    "NEAR/USDT:USDT",
    "NOT/USDT:USDT",
    "O/USDT:USDT",
    "ONDO/USDT:USDT",
    "OP/USDT:USDT",
    "ORDI/USDT:USDT",
    "PAXG/USDT:USDT",
    "PENDLE/USDT:USDT",
    "PENGU/USDT:USDT",
    "POPCAT/USDT:USDT",
    "PYTH/USDT:USDT",
    "RE/USDT:USDT",
    "RENDER/USDT:USDT",
    "RPL/USDT:USDT",
    "S/USDT:USDT",
    "SEI/USDT:USDT",
    "SHIB1000/USDT:USDT",
    "SNX/USDT:USDT",
    "SOL/USDT:USDT",
    "SPX/USDT:USDT",
    "STRK/USDT:USDT",
    "SUI/USDT:USDT",
    "TAIKO/USDT:USDT",
    "TAO/USDT:USDT",
    "THETA/USDT:USDT",
    "TIA/USDT:USDT",
    "TLM/USDT:USDT",
    "UNI/USDT:USDT",
    "VIRTUAL/USDT:USDT",
    "W/USDT:USDT",
    "WIF/USDT:USDT",
    "WLD/USDT:USDT",
    "XLM/USDT:USDT",
    "XMR/USDT:USDT",
    "XRP/USDT:USDT",
    "XTZ/USDT:USDT",
    "YFI/USDT:USDT",
    "ZEC/USDT:USDT",
    "ZEREBRO/USDT:USDT",
    "ZK/USDT:USDT",
)
LEGACY_DEFAULT_PAIRS: tuple[str, ...] = (
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
DEFAULT_STAKE = 5.0
DEFAULT_LEVERAGE = 3.0
DEFAULT_MAX_OPEN = 1
DEFAULT_MIN_SCORE = 6
DEFAULT_MIN_RR = 1.5
DEFAULT_MAX_SPREAD_PCT = 0.08
DEFAULT_MIN_DEPTH_USDT = 20_000.0
DEFAULT_MIN_TURNOVER_USDT = 20_000_000.0
DEFAULT_MAX_RISK_PCT = 2.5
DEFAULT_FUNDING_INTERVALS = 1.0
MAX_OPEN_LIMIT = 10
STAKE_MIN = 1.0
STAKE_MAX = 100.0
LEVERAGE_MIN = 1.0
LEVERAGE_MAX = 10.0
MAX_PAIRS = 99
_BASE_RE = re.compile(r"^[A-Z0-9]{1,20}$")


def default_pairs() -> list[str]:
    return list(DEFAULT_PAIRS)


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


def _clamp(value: float, lo: float, hi: float, *, digits: int = 4) -> float:
    return round(min(hi, max(lo, float(value))), digits)


def _num(data: dict[str, Any], key: str, default: float) -> float:
    raw = data.get(key)
    if raw is None or raw == "":
        return float(default)
    return float(raw)


def _flag(data: dict[str, Any], key: str, default: bool) -> bool:
    raw = data.get(key)
    if raw is None or raw == "":
        return bool(default)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def setup_enabled(settings: dict[str, Any] | None, setup: str) -> bool:
    key = SETUP_FLAGS.get(str(setup or ""))
    if key is None:
        return True
    return bool((settings or {}).get(key, True))


def setup_for_tag(tag: str) -> str:
    text = str(tag or "")
    for prefix, setup in TAG_TO_SETUP.items():
        if text.startswith(prefix):
            return setup
    return ""


def normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    pairs = normalize_pairs(data["pairs"]) if data.get("pairs") is not None else default_pairs()
    return {
        "enable_trend_pullback": _flag(
            data, "enable_trend_pullback", DEFAULT_ENABLE_TREND_PULLBACK
        ),
        "enable_liquidity_sweep": _flag(
            data, "enable_liquidity_sweep", DEFAULT_ENABLE_LIQUIDITY_SWEEP
        ),
        "stake_amount": _clamp(_num(data, "stake_amount", DEFAULT_STAKE), STAKE_MIN, STAKE_MAX, digits=2),
        "leverage": _clamp(_num(data, "leverage", DEFAULT_LEVERAGE), LEVERAGE_MIN, LEVERAGE_MAX, digits=2),
        "max_open_trades": int(
            min(MAX_OPEN_LIMIT, max(0, int(_num(data, "max_open_trades", DEFAULT_MAX_OPEN))))
        ),
        "min_score": int(_clamp(_num(data, "min_score", DEFAULT_MIN_SCORE), 4, 8, digits=0)),
        "min_rr": _clamp(_num(data, "min_rr", DEFAULT_MIN_RR), 0.8, 4.0, digits=2),
        "max_spread_pct": _clamp(
            _num(data, "max_spread_pct", DEFAULT_MAX_SPREAD_PCT), 0.02, 0.5, digits=3
        ),
        "min_depth_usdt": _clamp(
            _num(data, "min_depth_usdt", DEFAULT_MIN_DEPTH_USDT), 1_000, 5_000_000, digits=0
        ),
        "min_turnover_usdt": _clamp(
            _num(data, "min_turnover_usdt", DEFAULT_MIN_TURNOVER_USDT),
            1_000_000,
            2_000_000_000,
            digits=0,
        ),
        "max_risk_pct": _clamp(
            _num(data, "max_risk_pct", DEFAULT_MAX_RISK_PCT), 0.3, 5.0, digits=2
        ),
        "funding_intervals": _clamp(
            _num(data, "funding_intervals", DEFAULT_FUNDING_INTERVALS), 0.0, 3.0, digits=2
        ),
        "pairs": pairs,
    }


def payload(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    data = normalize(settings)
    out = dict(data)
    out["pairs"] = list(data["pairs"])
    return out


def load(user_id: str) -> dict[str, Any]:
    raw = cp.get_user_setting(str(user_id), KEY, None)
    if isinstance(raw, dict) and raw.get("pairs") is not None:
        try:
            stored_pairs = normalize_pairs(raw["pairs"])
        except (TypeError, ValueError):
            stored_pairs = []
        if len(stored_pairs) == len(LEGACY_DEFAULT_PAIRS) and set(stored_pairs) == set(
            LEGACY_DEFAULT_PAIRS
        ):
            raw = {**raw, "pairs": default_pairs()}
            cp.set_user_setting(str(user_id), KEY, normalize(raw))
    return payload(raw if isinstance(raw, dict) else None)


def save(user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    current = load(user_id)
    patch = patch if isinstance(patch, dict) else {}
    merged = {**current}
    for key in (
        "enable_trend_pullback",
        "enable_liquidity_sweep",
        "stake_amount",
        "leverage",
        "max_open_trades",
        "min_score",
        "min_rr",
        "max_spread_pct",
        "min_depth_usdt",
        "min_turnover_usdt",
        "max_risk_pct",
        "funding_intervals",
        "pairs",
    ):
        if key in patch:
            merged[key] = patch[key]
    stored = normalize(merged)
    cp.set_user_setting(str(user_id), KEY, stored)
    return payload(stored)


def pairs_for(user_id: str) -> list[str]:
    return list(load(user_id)["pairs"])


def floor_params(user_ids: list[str]) -> dict[str, Any]:
    """Most permissive thresholds among active users.

    The bot publishes a card when at least one trader could take it; the
    executor applies each user's own stricter settings afterwards.
    """
    rows = [load(uid) for uid in user_ids] if user_ids else []
    base = normalize(None)
    if not rows:
        return payload(base)
    base["enable_trend_pullback"] = any(bool(r["enable_trend_pullback"]) for r in rows)
    base["enable_liquidity_sweep"] = any(bool(r["enable_liquidity_sweep"]) for r in rows)
    base["min_score"] = min(int(r["min_score"]) for r in rows)
    base["min_rr"] = min(float(r["min_rr"]) for r in rows)
    base["max_spread_pct"] = max(float(r["max_spread_pct"]) for r in rows)
    base["min_depth_usdt"] = min(float(r["min_depth_usdt"]) for r in rows)
    base["min_turnover_usdt"] = min(float(r["min_turnover_usdt"]) for r in rows)
    base["max_risk_pct"] = max(float(r["max_risk_pct"]) for r in rows)
    base["funding_intervals"] = max(float(r["funding_intervals"]) for r in rows)
    return payload(base)


def analyze_kwargs(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "enable_trend_pullback": bool(params["enable_trend_pullback"]),
        "enable_liquidity_sweep": bool(params["enable_liquidity_sweep"]),
        "min_score": int(params["min_score"]),
        "min_rr": float(params["min_rr"]),
        "max_spread_pct": float(params["max_spread_pct"]),
        "min_depth_usdt": float(params["min_depth_usdt"]),
        "min_turnover_usdt": float(params["min_turnover_usdt"]),
        "max_risk_pct": float(params["max_risk_pct"]),
        "funding_intervals": float(params["funding_intervals"]),
    }


def trade_ratios(entry: float, sl: float, tp: float, leverage: float) -> tuple[float, float] | None:
    """Convert absolute prices to stake-PnL ratios (the executor's convention)."""
    entry = float(entry)
    if entry <= 0 or sl <= 0 or tp <= 0 or sl == entry:
        return None
    lev = max(1.0, float(leverage or 1.0))
    risk = abs(entry - sl) / entry
    reward = abs(tp - entry) / entry
    if risk <= 0 or reward <= 0:
        return None
    return -risk * lev, reward * lev

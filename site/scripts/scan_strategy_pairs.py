#!/usr/bin/env python3
"""
Scan Bybit USDT linear futures for pairs suitable for enabled technical strategies.

For each enabled strategy in enabled_strategies.json, applies strategy-specific
filters (trend / mean-reversion / momentum), picks top pairs per strategy,
then merges into config_strategy.json whitelist.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import talib.abstract as ta

BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers?category=linear"
BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"
INTERVAL_MAP = {"1m": "1", "5m": "5", "15m": "15", "1h": "60"}

STRATEGY_IDS = [
    "CriptoPairsStrategy",
    "SupertrendStrategy",
    "MacdEmaStrategy",
    "FibPullbackStrategy",
    "TripleEmaStrategy",
    "BollingerRsiStrategy",
    "AdxMomentumStrategy",
    "LiteIntradayStrategy",
    "LiteRangeStrategy",
]


def ft_base() -> Path:
    return Path(os.environ.get("CT_BASE", "/home/cryptotools/app"))


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip().rstrip("\r")
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def http_get(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def http_post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str] | None = None, timeout: int = 30
) -> Any:
    body = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status == 204:
            return None
        return json.loads(resp.read().decode())


def bybit_symbol_to_ft(symbol: str) -> str:
    base = symbol[: -len("USDT")]
    return f"{base}/USDT:USDT"


def fetch_tickers() -> list[dict[str, Any]]:
    data = http_get(BYBIT_TICKERS)
    return data.get("result", {}).get("list", [])


def fetch_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    q = urllib.parse.urlencode(
        {"category": "linear", "symbol": symbol, "interval": interval, "limit": str(limit)}
    )
    data = http_get(f"{BYBIT_KLINE}?{q}")
    rows = data.get("result", {}).get("list", [])
    if not rows:
        return pd.DataFrame()
    rows = list(reversed(rows))
    df = pd.DataFrame(
        rows, columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"]
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["close"], inplace=True)
    return df


def compute_pair_metrics(
    df: pd.DataFrame,
    *,
    bb_period: int,
    adx_period: int,
    rsi_period: int,
) -> dict[str, float] | None:
    min_len = max(bb_period, adx_period, rsi_period, 55, 200) + 5
    if len(df) < min_len:
        return None

    close = df["close"]
    boll_mid = close.rolling(bb_period).mean()
    boll_std = close.rolling(bb_period).std()
    bb_width = (4 * boll_std) / boll_mid.replace(0, np.nan)
    adx = ta.ADX(df, timeperiod=adx_period)
    rsi = ta.RSI(df, timeperiod=rsi_period)
    plus_di = ta.PLUS_DI(df, timeperiod=adx_period)
    minus_di = ta.MINUS_DI(df, timeperiod=adx_period)

    ema8 = ta.EMA(df, timeperiod=8)
    ema21 = ta.EMA(df, timeperiod=21)
    ema55 = ta.EMA(df, timeperiod=55)
    ema200 = ta.EMA(df, timeperiod=200)

    bull_stack = (ema8 > ema21) & (ema21 > ema55)
    bear_stack = (ema8 < ema21) & (ema21 < ema55)
    stack_ratio = float((bull_stack | bear_stack).tail(24).mean())

    ema200_last = float(ema200.iloc[-1])
    close_last = float(close.iloc[-1])
    ema200_dist = abs(close_last - ema200_last) / close_last if close_last else 0.0
    ema200_prev = float(ema200.iloc[-11]) if pd.notna(ema200.iloc[-11]) else ema200_last
    ema200_slope = abs(ema200_last - ema200_prev) / ema200_prev if ema200_prev else 0.0

    rsi_tail = rsi.tail(24).dropna()
    rsi_swing = float(rsi_tail.max() - rsi_tail.min()) if len(rsi_tail) >= 8 else 0.0

    last_bb = float(bb_width.iloc[-1]) if pd.notna(bb_width.iloc[-1]) else 0.0
    last_adx = float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else 0.0
    last_rsi = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else 50.0
    last_plus = float(plus_di.iloc[-1]) if pd.notna(plus_di.iloc[-1]) else 0.0
    last_minus = float(minus_di.iloc[-1]) if pd.notna(minus_di.iloc[-1]) else 0.0
    di_spread = abs(last_plus - last_minus)

    valid = bb_width.notna() & adx.notna()
    if not valid.any():
        return None

    return {
        "bb_width": last_bb,
        "adx": last_adx,
        "rsi": last_rsi,
        "plus_di": last_plus,
        "minus_di": last_minus,
        "di_spread": di_spread,
        "ema200_dist": ema200_dist,
        "ema200_slope": ema200_slope,
        "stack_ratio": stack_ratio,
        "rsi_swing": rsi_swing,
        "bb_series": bb_width,
        "adx_series": adx,
    }


def active_ratio(
    metrics: dict[str, Any],
    *,
    min_bb: float,
    max_bb: float,
    min_adx: float,
    max_adx: float,
) -> float:
    bb = metrics["bb_series"]
    adx = metrics["adx_series"]
    active = (bb >= min_bb) & (bb <= max_bb) & (adx >= min_adx) & (adx <= max_adx)
    valid = active.notna() & bb.notna() & adx.notna()
    if not valid.any():
        return 0.0
    return float(active[valid].sum() / valid.sum())


def score_strategy(strategy_id: str, metrics: dict[str, Any], profile: dict[str, Any]) -> float | None:
    min_bb = float(profile.get("min_bb_width", 0.01))
    max_bb = float(profile.get("max_bb_width", 0.12))
    min_adx = float(profile.get("min_adx", 10))
    max_adx = float(profile.get("max_adx", 50))

    bb = metrics["bb_width"]
    adx = metrics["adx"]
    rsi = metrics["rsi"]

    if bb < min_bb or bb > max_bb:
        return None
    if adx < min_adx or adx > max_adx:
        return None

    ratio = active_ratio(metrics, min_bb=min_bb, max_bb=max_bb, min_adx=min_adx, max_adx=max_adx)
    min_ratio = float(profile.get("min_active_ratio", 0.45))
    if ratio < min_ratio:
        return None

    if strategy_id == "CriptoPairsStrategy":
        rsi_min = float(profile.get("rsi_min", 28))
        rsi_max = float(profile.get("rsi_max", 72))
        if rsi < rsi_min or rsi > rsi_max:
            return None
        min_swing = float(profile.get("min_rsi_swing", 6))
        if metrics["rsi_swing"] < min_swing:
            return None
        rsi_room = min(abs(rsi - 35), abs(rsi - 65)) / 35
        return bb * (0.4 + 0.35 * rsi_room + 0.25 * ratio)

    if strategy_id == "SupertrendStrategy":
        rsi_min = float(profile.get("rsi_min", 40))
        rsi_max = float(profile.get("rsi_max", 70))
        if rsi < rsi_min or rsi > rsi_max:
            return None
        sweet = float(profile.get("adx_sweet", 28))
        adx_fit = 1.0 - min(abs(adx - sweet) / sweet, 1.0)
        return bb * (0.35 + 0.45 * adx_fit + 0.2 * ratio)

    if strategy_id == "MacdEmaStrategy":
        if metrics["ema200_dist"] < float(profile.get("min_ema200_dist", 0.004)):
            return None
        if metrics["ema200_slope"] < float(profile.get("min_ema200_slope", 0.0008)):
            return None
        trend_fit = min(metrics["ema200_dist"] * 40, 1.0)
        adx_fit = min(adx / 35, 1.0)
        return bb * (0.3 + 0.35 * trend_fit + 0.35 * adx_fit) * (0.5 + 0.5 * ratio)

    if strategy_id == "TripleEmaStrategy":
        if metrics["stack_ratio"] < float(profile.get("min_stack_ratio", 0.55)):
            return None
        stack_fit = metrics["stack_ratio"]
        adx_fit = 1.0 - min(abs(adx - 24) / 24, 1.0)
        return bb * (0.35 + 0.4 * stack_fit + 0.25 * adx_fit) * (0.5 + 0.5 * ratio)

    if strategy_id == "FibPullbackStrategy":
        if metrics["stack_ratio"] < float(profile.get("min_stack_ratio", 0.5)):
            return None
        stack_fit = metrics["stack_ratio"]
        adx_fit = 1.0 - min(abs(adx - 26) / 26, 1.0)
        return bb * (0.35 + 0.4 * stack_fit + 0.25 * adx_fit) * (0.5 + 0.5 * ratio)

    if strategy_id == "BollingerRsiStrategy":
        rsi_min = float(profile.get("rsi_min", 25))
        rsi_max = float(profile.get("rsi_max", 75))
        if rsi < rsi_min or rsi > rsi_max:
            return None
        min_swing = float(profile.get("min_rsi_swing", 8))
        if metrics["rsi_swing"] < min_swing:
            return None
        range_fit = 1.0 - min(adx / max_adx, 1.0)
        swing_fit = min(metrics["rsi_swing"] / 30, 1.0)
        return bb * (0.35 + 0.4 * range_fit + 0.25 * swing_fit) * (0.5 + 0.5 * ratio)

    if strategy_id == "AdxMomentumStrategy":
        if metrics["di_spread"] < float(profile.get("min_di_spread", 4)):
            return None
        sweet = float(profile.get("adx_sweet", 32))
        adx_fit = 1.0 - min(abs(adx - sweet) / sweet, 1.0)
        di_fit = min(metrics["di_spread"] / 25, 1.0)
        return bb * (0.3 + 0.4 * adx_fit + 0.3 * di_fit) * (0.5 + 0.5 * ratio)

    if strategy_id == "LiteIntradayStrategy":
        sweet = float(profile.get("adx_sweet", 19))
        adx_fit = 1.0 - min(abs(adx - sweet) / 5, 1.0)
        vol_fit = min(ratio / 1.35, 1.0)
        return bb * (0.35 + 0.45 * adx_fit + 0.2 * vol_fit)

    if strategy_id == "LiteRangeStrategy":
        if adx >= float(profile.get("max_adx", 18)):
            return None
        width = metrics.get("bb_width", bb)
        if width < float(profile.get("min_bb_width", 0.015)) or width > float(profile.get("max_bb_width", 0.055)):
            return None
        range_fit = 1.0 - min(adx / 18, 1.0)
        return bb * (0.4 + 0.35 * range_fit + 0.25 * min(ratio / 1.25, 1.0))

    return None


def load_enabled_strategies(base: Path, cfg: dict[str, Any]) -> list[str]:
    path = base / cfg.get("enabled_strategies_file", "user_data/enabled_strategies.json")
    enabled_map: dict[str, bool] = {sid: sid == "CriptoPairsStrategy" for sid in STRATEGY_IDS}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        stored = data.get("enabled", {})
        for sid in STRATEGY_IDS:
            if sid in stored:
                enabled_map[sid] = bool(stored[sid])
    enabled = [sid for sid, on in enabled_map.items() if on]
    return enabled or ["CriptoPairsStrategy"]


def strategy_api_token(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    data = http_post_json(
        "http://127.0.0.1:8081/api/v1/token/login",
        {},
        headers={"Authorization": f"Basic {token}"},
    )
    return data["access_token"]


def strategy_open_pairs(user: str, password: str) -> list[str]:
    try:
        access = strategy_api_token(user, password)
        status = http_get(
            "http://127.0.0.1:8081/api/v1/status",
            headers={"Authorization": f"Bearer {access}"},
        )
        if isinstance(status, list):
            return sorted({t["pair"] for t in status if t.get("is_open")})
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, json.JSONDecodeError):
        pass
    return []


def reload_strategy(url: str, user: str, password: str) -> bool:
    try:
        access = strategy_api_token(user, password)
        http_post_json(url, {}, headers={"Authorization": f"Bearer {access}"})
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, json.JSONDecodeError):
        return False


def load_scan_config(base: Path) -> dict[str, Any]:
    path = base / "user_data" / "strategy_scan_config.json"
    defaults: dict[str, Any] = {
        "timeframe": "5m",
        "bb_period": 20,
        "adx_period": 14,
        "rsi_period": 14,
        "min_turnover24h_usd": 3_000_000,
        "scan_top_volume": 100,
        "max_pairs": 12,
        "pairs_per_strategy": None,
        "lookback_candles": 48,
        "exclude_symbols": ["USDCUSDT", "USDEUSDT"],
        "exclude_bases": ["BTC", "ETH"],
        "enabled_strategies_file": "user_data/enabled_strategies.json",
        "strategy_config": "user_data/config_strategy.json",
        "strategy_pairs_file": "user_data/strategy_pairs.json",
        "strategy_reload_url": "http://127.0.0.1:8081/api/v1/reload_config",
        "keep_open_trade_pairs": True,
        "strategy_profiles": {},
    }
    if path.is_file():
        stored = json.loads(path.read_text(encoding="utf-8"))
        defaults.update(stored)
    return defaults


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


def run_scan(dry_run: bool = False, verbose: bool = False) -> dict[str, Any]:
    base = ft_base()
    cfg = load_scan_config(base)
    env = load_env_file(Path(os.environ.get("CT_ENV", "/home/cryptotools/.cryptotools.env")))
    api_user = os.environ.get("FREQUI_USERNAME") or env.get("FREQUI_USERNAME", "cryptotools")
    api_pass = os.environ.get("FREQUI_PASSWORD") or env.get("FREQUI_PASSWORD", "")

    enabled_ids = load_enabled_strategies(base, cfg)
    profiles: dict[str, dict[str, Any]] = cfg.get("strategy_profiles", {})

    interval = INTERVAL_MAP.get(cfg["timeframe"], "5")
    lookback = int(cfg["lookback_candles"])
    max_pairs = int(cfg["max_pairs"])
    scan_top = int(cfg["scan_top_volume"])
    min_vol = float(cfg["min_turnover24h_usd"])
    exclude_sym = set(cfg.get("exclude_symbols", []))
    exclude_bases = set(cfg.get("exclude_bases", []))

    per_strategy = cfg.get("pairs_per_strategy")
    if per_strategy is None:
        per_strategy = max(2, math.ceil(max_pairs / len(enabled_ids)))
    else:
        per_strategy = int(per_strategy)

    tickers = fetch_tickers()
    candidates: list[tuple[float, str]] = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym in exclude_sym:
            continue
        base_coin = sym[: -len("USDT")]
        if base_coin in exclude_bases:
            continue
        vol = float(t.get("turnover24h") or 0)
        if vol >= min_vol:
            candidates.append((vol, sym))
    candidates.sort(reverse=True)
    candidates = candidates[:scan_top]

    if verbose:
        print(
            f"Strategy scan: {len(candidates)} pairs, "
            f"{len(enabled_ids)} enabled strategies, {per_strategy} pairs each"
        )
        print("  Enabled:", ", ".join(enabled_ids))

    # per-strategy buckets
    by_strategy: dict[str, list[dict[str, Any]]] = {sid: [] for sid in enabled_ids}
    pair_scores: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    bb_period = int(cfg["bb_period"])
    adx_period = int(cfg["adx_period"])
    rsi_period = int(cfg["rsi_period"])
    kline_limit = lookback + max(bb_period, adx_period, rsi_period, 55, 200) + 15

    for i, (vol, sym) in enumerate(candidates):
        try:
            df = fetch_klines(sym, interval, kline_limit)
            metrics = compute_pair_metrics(
                df,
                bb_period=bb_period,
                adx_period=adx_period,
                rsi_period=rsi_period,
            )
            if metrics is None:
                continue

            pair = bybit_symbol_to_ft(sym)
            matched: list[str] = []

            for sid in enabled_ids:
                profile = profiles.get(sid, {})
                score = score_strategy(sid, metrics, profile)
                if score is None:
                    continue
                matched.append(sid)
                entry = {
                    "pair": pair,
                    "symbol": sym,
                    "turnover24h": vol,
                    "strategy": sid,
                    "score": round(score, 6),
                    "adx": round(metrics["adx"], 2),
                    "bb_width": round(metrics["bb_width"], 4),
                    "rsi": round(metrics["rsi"], 1),
                }
                by_strategy[sid].append(entry)

            if not matched:
                continue

            best_score = max(
                score_strategy(sid, metrics, profiles.get(sid, {})) or 0.0 for sid in matched
            )
            existing = pair_scores.get(pair)
            if existing is None or best_score > existing["score"]:
                pair_scores[pair] = {
                    "pair": pair,
                    "symbol": sym,
                    "turnover24h": vol,
                    "score": round(best_score, 6),
                    "adx": round(metrics["adx"], 2),
                    "bb_width": round(metrics["bb_width"], 4),
                    "rsi": round(metrics["rsi"], 1),
                    "strategies": matched,
                }
            elif existing is not None:
                for sid in matched:
                    if sid not in existing["strategies"]:
                        existing["strategies"].append(sid)

            if verbose and matched:
                print(
                    f"  OK {pair}: {', '.join(matched)} "
                    f"ADX={metrics['adx']:.1f} BBw={metrics['bb_width']:.3f}"
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{sym}: {exc}")
        if i % 10 == 9:
            time.sleep(0.3)

    selected_pairs: list[str] = []
    strategy_selections: dict[str, list[str]] = {}

    for sid in enabled_ids:
        bucket = sorted(by_strategy[sid], key=lambda x: x["score"], reverse=True)
        picks = [e["pair"] for e in bucket[:per_strategy]]
        strategy_selections[sid] = picks
        for p in picks:
            if p not in selected_pairs:
                selected_pairs.append(p)

    if len(selected_pairs) > max_pairs:
        ranked = sorted(pair_scores.values(), key=lambda x: x["score"], reverse=True)
        selected_pairs = [r["pair"] for r in ranked[:max_pairs]]

    keep_pairs: list[str] = []
    if cfg.get("keep_open_trade_pairs") and api_pass:
        keep_pairs = strategy_open_pairs(api_user, api_pass)

    final_pairs = list(dict.fromkeys(selected_pairs + keep_pairs))
    suitable_found = len(pair_scores)

    report = {
        "scanned_at": datetime.now(UTC).isoformat(),
        "candidates_checked": len(candidates),
        "suitable_found": suitable_found,
        "selected_count": len(selected_pairs),
        "enabled_strategies": enabled_ids,
        "pairs_per_strategy": per_strategy,
        "by_strategy": {
            sid: {
                "found": len(by_strategy[sid]),
                "selected": strategy_selections.get(sid, []),
                "top_pairs": sorted(by_strategy[sid], key=lambda x: x["score"], reverse=True)[
                    :per_strategy
                ],
            }
            for sid in enabled_ids
        },
        "whitelist": final_pairs,
        "pairs": [pair_scores[p] for p in selected_pairs if p in pair_scores],
        "kept_open_trades": keep_pairs,
        "errors": errors[:20],
    }

    out_file = base / cfg["strategy_pairs_file"]
    save_json(out_file, report)

    if verbose:
        print(f"\nSelected {len(selected_pairs)} pairs ({suitable_found} suitable total)")
        for sid in enabled_ids:
            n = len(strategy_selections.get(sid, []))
            print(f"  {sid}: {n} pairs")
        for p in final_pairs:
            tags = pair_scores.get(p, {}).get("strategies", [])
            extra = f" [{', '.join(tags)}]" if tags else ""
            print(f"  • {p}{extra}")

    if dry_run:
        return report

    if cfg.get("apply_whitelist") is False:
        if verbose:
            print("apply_whitelist=false — strategy config not updated")
        return report

    strat_cfg_path = base / cfg["strategy_config"]
    strat_cfg = json.loads(strat_cfg_path.read_text(encoding="utf-8"))
    old_wl = list(strat_cfg.get("exchange", {}).get("pair_whitelist", []))
    if old_wl != final_pairs:
        strat_cfg.setdefault("exchange", {})["pair_whitelist"] = final_pairs
        save_json(strat_cfg_path, strat_cfg)
        if verbose:
            print(f"Updated {strat_cfg_path.name}: {len(old_wl)} -> {len(final_pairs)} pairs")
        if api_pass:
            ok = reload_strategy(cfg["strategy_reload_url"], api_user, api_pass)
            if verbose:
                print("Strategy reload:", "OK" if ok else "failed (restart bot manually)")
    elif verbose:
        print("Strategy whitelist unchanged")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan Bybit for strategy-suitable pairs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args()
    try:
        report = run_scan(dry_run=args.dry_run, verbose=args.verbose and not args.quiet)
        print(
            json.dumps(
                {
                    "whitelist": report["whitelist"],
                    "found": report["suitable_found"],
                    "enabled": report["enabled_strategies"],
                }
            )
        )
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Scan Bybit USDT linear futures for true sideways (ranging) markets — Grid bot whitelist.

Scanner v3 filters:
  - ADX below adx_max (weak trend) + ADX not rising fast
  - BB width in band + not in squeeze / not in vol expansion vs own history
  - Price mostly inside Bollinger bands
  - Flat EMA50 slope on entry TF
  - ATR ratio in range (no volatility explosion, not dead)
  - Range structure: midline crosses + stable channel
  - Optional 1h confirmation (MTF ADX + flat EMA)

Updates config_grid.json pair_whitelist and feeds Bybit grid scan.
"""
from __future__ import annotations

import argparse
import base64
import json
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


def ft_base() -> Path:
    return Path(os.environ.get("FT_BASE", "/home/freqtrade/freqtrade"))


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


def fetch_htf_klines(symbol: str, cfg: dict[str, Any]) -> pd.DataFrame | None:
    if not cfg.get("mtf_enabled", True):
        return None
    htf = str(cfg.get("htf_timeframe", "1h"))
    interval = INTERVAL_MAP.get(htf, "60")
    lookback = int(cfg.get("htf_lookback_candles", 48))
    return fetch_klines(symbol, interval, lookback + 15)


def _ranging_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "adx_max": 22.0,
        "bb_width_min": 0.012,
        "bb_width_max": 0.06,
        "bb_period": 20,
        "adx_period": 14,
        "ema_period": 50,
        "max_ema50_slope": 0.0012,
        "min_inside_bb_ratio": 0.72,
        "min_ranging_ratio": 0.65,
        "ideal_bb_width": 0.028,
        "atr_period": 14,
        "atr_sma_period": 20,
        "max_atr_ratio": 1.35,
        "min_atr_ratio": 0.55,
        "bb_squeeze_percentile": 12.0,
        "bb_width_percentile_min": 18.0,
        "bb_width_percentile_max": 82.0,
        "max_adx_slope": 1.5,
        "min_midline_crosses": 3,
        "min_range_stability": 0.55,
        "mtf_enabled": True,
        "mtf_required": False,
        "htf_timeframe": "15m",
        "htf_lookback_candles": 48,
        "htf_adx_max": 24.0,
        "htf_max_ema_slope": 0.0025,
    }
    return {**defaults, **{k: cfg[k] for k in defaults if k in cfg}}


def _range_structure(df: pd.DataFrame, lookback: int) -> dict[str, float]:
    window = df.tail(lookback)
    if len(window) < 10:
        return {"midline_crosses": 0.0, "range_stability": 0.0, "range_pct": 0.0}
    high = window["high"]
    low = window["low"]
    close = window["close"]
    range_high = float(high.max())
    range_low = float(low.min())
    mid = (range_high + range_low) / 2.0
    span = range_high - range_low
    if span <= 0 or mid <= 0:
        return {"midline_crosses": 0.0, "range_stability": 0.0, "range_pct": 0.0}
    above = close > mid
    crosses = float((above != above.shift(1)).sum())
    half = max(len(window) // 2, 1)
    r1 = float(window.iloc[:half]["high"].max() - window.iloc[:half]["low"].min())
    r2 = float(window.iloc[half:]["high"].max() - window.iloc[half:]["low"].min())
    stability = min(r1, r2) / max(r1, r2) if max(r1, r2) > 0 else 0.0
    return {
        "midline_crosses": crosses,
        "range_stability": stability,
        "range_pct": span / mid,
    }


def analyze_htf_ranging(df_htf: pd.DataFrame, cfg: dict[str, Any]) -> bool:
    p = _ranging_cfg(cfg)
    min_len = max(int(p["adx_period"]), int(p["ema_period"])) + 5
    if len(df_htf) < min_len:
        return False
    adx = ta.ADX(df_htf, timeperiod=int(p["adx_period"]))
    ema = ta.EMA(df_htf, timeperiod=int(p["ema_period"]))
    last_adx = float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else 999.0
    if last_adx >= float(p["htf_adx_max"]):
        return False
    ema_last = float(ema.iloc[-1]) if pd.notna(ema.iloc[-1]) else 0.0
    ema_prev = float(ema.iloc[-11]) if len(ema) > 11 and pd.notna(ema.iloc[-11]) else ema_last
    ema_slope = abs(ema_last - ema_prev) / ema_prev if ema_prev else 999.0
    return ema_slope <= float(p["htf_max_ema_slope"])


def analyze_ranging(
    df: pd.DataFrame,
    cfg: dict[str, Any],
    *,
    df_htf: pd.DataFrame | None = None,
) -> dict[str, float] | None:
    p = _ranging_cfg(cfg)
    bb_period = int(p["bb_period"])
    adx_period = int(p["adx_period"])
    ema_period = int(p["ema_period"])
    adx_max = float(p["adx_max"])
    bb_width_min = float(p["bb_width_min"])
    bb_width_max = float(p["bb_width_max"])
    min_inside_bb_ratio = float(p["min_inside_bb_ratio"])

    min_len = max(bb_period, adx_period, ema_period, int(p["atr_sma_period"])) + 5
    if len(df) < min_len:
        return None

    if cfg.get("mtf_required", False):
        if df_htf is None or len(df_htf) < min_len:
            return None
        if not analyze_htf_ranging(df_htf, cfg):
            return None

    htf_confirmed = True
    if cfg.get("mtf_enabled", True) and df_htf is not None and len(df_htf) >= min_len:
        htf_confirmed = analyze_htf_ranging(df_htf, cfg)

    close = df["close"]
    boll_mid = close.rolling(bb_period).mean()
    boll_std = close.rolling(bb_period).std()
    upper = boll_mid + 2 * boll_std
    lower = boll_mid - 2 * boll_std
    bb_width = (upper - lower) / boll_mid.replace(0, np.nan)
    adx = ta.ADX(df, timeperiod=adx_period)
    ema = ta.EMA(df, timeperiod=ema_period)
    atr = ta.ATR(df, timeperiod=int(p["atr_period"]))
    atr_sma = atr.rolling(int(p["atr_sma_period"])).mean()

    inside_bb = (close >= lower) & (close <= upper)
    ranging = (
        (adx < adx_max)
        & (bb_width >= bb_width_min)
        & (bb_width <= bb_width_max)
        & inside_bb
    )
    valid = ranging.notna() & bb_width.notna() & adx.notna()
    if not valid.any():
        return None

    last_adx = float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else 999.0
    last_bb = float(bb_width.iloc[-1]) if pd.notna(bb_width.iloc[-1]) else 0.0
    ranging_ratio = float(ranging[valid].sum() / valid.sum())
    inside_ratio = float(inside_bb[valid].sum() / valid.sum())

    ema_last = float(ema.iloc[-1]) if pd.notna(ema.iloc[-1]) else 0.0
    ema_prev = float(ema.iloc[-11]) if len(ema) > 11 and pd.notna(ema.iloc[-11]) else ema_last
    ema_slope = abs(ema_last - ema_prev) / ema_prev if ema_prev else 999.0

    atr_last = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else 0.0
    atr_base = float(atr_sma.iloc[-1]) if pd.notna(atr_sma.iloc[-1]) else 0.0
    atr_ratio = atr_last / atr_base if atr_base > 0 else 999.0

    bb_hist = bb_width.dropna()
    bb_pctile = float(bb_hist.rank(pct=True).iloc[-1] * 100.0) if len(bb_hist) >= bb_period else 50.0

    adx_hist = adx.dropna()
    adx_slope = (
        float(adx_hist.iloc[-1] - adx_hist.iloc[-6]) if len(adx_hist) >= 6 else 0.0
    )

    lookback = int(cfg.get("lookback_candles", 96))
    structure = _range_structure(df, lookback)

    if last_adx >= adx_max:
        return None
    if last_bb < bb_width_min or last_bb > bb_width_max:
        return None
    if inside_ratio < min_inside_bb_ratio:
        return None
    if ema_slope > float(p["max_ema50_slope"]):
        return None
    if atr_ratio > float(p["max_atr_ratio"]) or atr_ratio < float(p["min_atr_ratio"]):
        return None
    if bb_pctile < float(p["bb_squeeze_percentile"]):
        return None
    if bb_pctile < float(p["bb_width_percentile_min"]) or bb_pctile > float(p["bb_width_percentile_max"]):
        return None
    if adx_slope > float(p["max_adx_slope"]):
        return None
    if structure["midline_crosses"] < float(p["min_midline_crosses"]):
        return None
    if structure["range_stability"] < float(p["min_range_stability"]):
        return None

    ideal_bb = float(p["ideal_bb_width"])
    adx_fit = (adx_max - last_adx) / adx_max
    bb_sweet = 1.0 - min(abs(last_bb - ideal_bb) / ideal_bb, 1.0)
    atr_fit = 1.0 - min(max(atr_ratio - 1.0, 0.0) / max(float(p["max_atr_ratio"]) - 1.0, 0.01), 1.0)
    structure_fit = min(structure["midline_crosses"] / float(p["min_midline_crosses"]), 1.0) * structure[
        "range_stability"
    ]
    bb_pct_fit = 1.0 - min(
        abs(bb_pctile - 50.0) / max(50.0 - float(p["bb_width_percentile_min"]), 1.0), 1.0
    )
    score = last_bb * (
        0.22 * adx_fit
        + 0.18 * bb_sweet
        + 0.16 * ranging_ratio
        + 0.14 * inside_ratio
        + 0.12 * atr_fit
        + 0.10 * structure_fit
        + 0.08 * bb_pct_fit
    )
    if cfg.get("mtf_enabled", True):
        score *= 1.12 if htf_confirmed else 0.82

    return {
        "adx": round(last_adx, 2),
        "bb_width": round(last_bb, 4),
        "ranging_ratio": round(ranging_ratio, 3),
        "inside_bb_ratio": round(inside_ratio, 3),
        "ema50_slope": round(ema_slope, 6),
        "atr_ratio": round(atr_ratio, 3),
        "bb_width_pctile": round(bb_pctile, 1),
        "adx_slope": round(adx_slope, 2),
        "midline_crosses": round(structure["midline_crosses"], 1),
        "range_stability": round(structure["range_stability"], 3),
        "htf_ok": htf_confirmed,
        "score": round(score, 6),
    }


def grid_api_token(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    data = http_post_json(
        "http://127.0.0.1:8082/api/v1/token/login",
        {},
        headers={"Authorization": f"Basic {token}"},
    )
    return data["access_token"]


def grid_open_pairs(user: str, password: str) -> list[str]:
    try:
        access = grid_api_token(user, password)
        status = http_get(
            "http://127.0.0.1:8082/api/v1/status",
            headers={"Authorization": f"Bearer {access}"},
        )
        if isinstance(status, list):
            return sorted({t["pair"] for t in status if t.get("is_open")})
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, json.JSONDecodeError):
        pass
    return []


def reload_grid(url: str, user: str, password: str) -> bool:
    try:
        access = grid_api_token(user, password)
        http_post_json(url, {}, headers={"Authorization": f"Bearer {access}"})
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, json.JSONDecodeError):
        return False


def load_scan_config(base: Path) -> dict[str, Any]:
    path = base / "user_data" / "ranging_scan_config.json"
    defaults: dict[str, Any] = {
        "timeframe": "5m",
        "adx_max": 22,
        "bb_width_min": 0.012,
        "bb_width_max": 0.06,
        "bb_period": 20,
        "adx_period": 14,
        "ema_period": 50,
        "max_ema50_slope": 0.0012,
        "min_turnover24h_usd": 5_000_000,
        "scan_top_volume": 100,
        "max_pairs": 10,
        "min_ranging_ratio": 0.65,
        "min_inside_bb_ratio": 0.72,
        "lookback_candles": 96,
        "ideal_bb_width": 0.028,
        "atr_period": 14,
        "atr_sma_period": 20,
        "max_atr_ratio": 1.35,
        "min_atr_ratio": 0.55,
        "bb_squeeze_percentile": 12,
        "bb_width_percentile_min": 18,
        "bb_width_percentile_max": 82,
        "max_adx_slope": 1.5,
        "min_midline_crosses": 3,
        "min_range_stability": 0.55,
        "mtf_enabled": True,
        "mtf_required": False,
        "htf_lookback_candles": 48,
        "htf_adx_max": 25,
        "htf_max_ema_slope": 0.002,
        "exclude_symbols": ["USDCUSDT", "USDEUSDT"],
        "exclude_bases": ["BTC", "ETH"],
        "priority_bases": [],
        "priority_score_boost": 1.25,
        "grid_config": "user_data/config_grid.json",
        "ranging_pairs_file": "user_data/ranging_pairs.json",
        "grid_reload_url": "http://127.0.0.1:8082/api/v1/reload_config",
        "keep_open_trade_pairs": True,
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
    env = load_env_file(Path(os.environ.get("FT_ENV", "/home/freqtrade/.freqtrade.env")))
    api_user = os.environ.get("FREQUI_USERNAME") or env.get("FREQUI_USERNAME", "freqtrader")
    api_pass = os.environ.get("FREQUI_PASSWORD") or env.get("FREQUI_PASSWORD", "")

    interval = INTERVAL_MAP.get(cfg["timeframe"], "5")
    lookback = int(cfg["lookback_candles"])
    adx_max = float(cfg["adx_max"])
    bb_width_min = float(cfg["bb_width_min"])
    bb_width_max = float(cfg["bb_width_max"])
    min_ratio = float(cfg["min_ranging_ratio"])
    scan_top = int(cfg["scan_top_volume"])
    min_vol = float(cfg["min_turnover24h_usd"])
    exclude_sym = set(cfg.get("exclude_symbols", []))
    exclude_bases = set(cfg.get("exclude_bases", []))
    priority_bases = set(cfg.get("priority_bases", []))
    priority_boost = float(cfg.get("priority_score_boost", 1.25))

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
            f"Ranging scan v3: {len(candidates)} pairs "
            f"(ADX<{adx_max}, BB {bb_width_min:.1%}-{bb_width_max:.1%}, "
            f"ATR, MTF={cfg.get('mtf_enabled', True)})..."
        )

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, (vol, sym) in enumerate(candidates):
        try:
            df = fetch_klines(sym, interval, lookback + 15)
            df_htf = fetch_htf_klines(sym, cfg)
            metrics = analyze_ranging(df, cfg, df_htf=df_htf)
            if metrics is None or metrics["ranging_ratio"] < min_ratio:
                continue
            base_coin = sym[: -len("USDT")]
            score = metrics["score"]
            if base_coin in priority_bases:
                score *= priority_boost
                metrics["priority"] = True
            else:
                metrics["priority"] = False
            metrics["score"] = round(score, 6)

            pair = bybit_symbol_to_ft(sym)
            entry = {"pair": pair, "symbol": sym, "turnover24h": vol, **metrics}
            results.append(entry)
            if verbose:
                pri = "*" if metrics["priority"] else ""
                print(
                    f"  OK{pri} {pair}: ADX={metrics['adx']} BBw={metrics['bb_width']:.3f} "
                    f"ATR×={metrics['atr_ratio']:.2f} x={metrics['midline_crosses']:.0f} "
                    f"score={metrics['score']:.5f}"
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{sym}: {exc}")
        if i % 10 == 9:
            time.sleep(0.055)

    results.sort(key=lambda x: x["score"], reverse=True)
    max_pairs = int(cfg["max_pairs"])
    selected = results[:max_pairs]

    keep_pairs: list[str] = []
    if cfg.get("keep_open_trade_pairs") and api_pass:
        keep_pairs = grid_open_pairs(api_user, api_pass)

    final_pairs = list(dict.fromkeys([r["pair"] for r in selected] + keep_pairs))

    grid_cfg_path = base / cfg["grid_config"]
    grid_cfg = json.loads(grid_cfg_path.read_text(encoding="utf-8"))
    old_wl = list(grid_cfg.get("exchange", {}).get("pair_whitelist", []))
    whitelist_kept = False
    if not final_pairs:
        fallback = [str(p) for p in cfg.get("fallback_pairs", []) if p]
        final_pairs = old_wl or fallback
        whitelist_kept = True
        if verbose:
            if old_wl:
                print(f"No ranging pairs — keeping previous whitelist ({len(old_wl)} pairs)")
            elif fallback:
                print(f"No ranging pairs — using fallback whitelist ({len(fallback)} pairs)")
            else:
                print("WARNING: No ranging pairs and no fallback — whitelist not cleared")

    report = {
        "scanned_at": datetime.now(UTC).isoformat(),
        "scanner_version": 3,
        "candidates_checked": len(candidates),
        "ranging_found": len(results),
        "selected_count": len(selected),
        "whitelist": final_pairs,
        "whitelist_kept": whitelist_kept,
        "pairs": selected,
        "kept_open_trades": keep_pairs,
        "errors": errors[:20],
        "params": {
            k: cfg[k]
            for k in (
                "adx_max",
                "bb_width_min",
                "bb_width_max",
                "min_ranging_ratio",
                "min_inside_bb_ratio",
                "max_ema50_slope",
                "lookback_candles",
                "max_atr_ratio",
                "min_atr_ratio",
                "bb_squeeze_percentile",
                "max_adx_slope",
                "min_midline_crosses",
                "min_range_stability",
                "mtf_enabled",
                "htf_timeframe",
                "htf_adx_max",
                "max_pairs",
            )
            if k in cfg
        },
    }

    ranging_file = base / cfg["ranging_pairs_file"]
    save_json(ranging_file, report)

    if verbose:
        print(f"\nSelected {len(selected)} ranging pairs, whitelist {len(final_pairs)} total")
        for p in final_pairs:
            print(f"  • {p}")

    if dry_run:
        return report

    if cfg.get("apply_whitelist") is False:
        if verbose:
            print("apply_whitelist=false — grid config not updated")
        return report

    if not final_pairs:
        if verbose:
            print("Grid whitelist left unchanged (would be empty)")
        return report

    if old_wl != final_pairs:
        grid_cfg.setdefault("exchange", {})["pair_whitelist"] = final_pairs
        save_json(grid_cfg_path, grid_cfg)
        if verbose:
            print(f"Updated {grid_cfg_path.name}: {len(old_wl)} -> {len(final_pairs)} pairs")
        if api_pass:
            ok = reload_grid(cfg["grid_reload_url"], api_user, api_pass)
            if verbose:
                print("Grid reload:", "OK" if ok else "failed (restart bot manually)")
    elif verbose:
        print("Grid whitelist unchanged")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan Bybit for ranging pairs and update Grid bot")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, do not update config")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args()
    try:
        report = run_scan(dry_run=args.dry_run, verbose=args.verbose and not args.quiet)
        print(json.dumps({"whitelist": report["whitelist"], "found": report["ranging_found"]}))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

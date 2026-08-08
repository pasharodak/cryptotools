#!/usr/bin/env python3
"""Probe live entry signals + ML confidence (run on VPS or locally)."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
USER_DATA = ROOT / "user_data"
STRAT_DIR = USER_DATA / "strategies"
if str(USER_DATA) not in sys.path:
    sys.path.insert(0, str(USER_DATA))
if str(STRAT_DIR) not in sys.path:
    sys.path.insert(0, str(STRAT_DIR))

from ml.gate import GRID_SCENARIO_ID, get_live_ml_gate  # noqa: E402
from ml.finder_live import FinderLive  # noqa: E402
from ml.market_features import features_from_ohlcv  # noqa: E402

from AdxMomentumStrategy import AdxMomentumStrategy  # noqa: E402
from BollingerRsiStrategy import BollingerRsiStrategy  # noqa: E402
from LiteIntradayStrategy import LiteIntradayStrategy  # noqa: E402
from LiteRangeStrategy import LiteRangeStrategy  # noqa: E402
from TripleEmaStrategy import TripleEmaStrategy  # noqa: E402
from VolatilityGridStrategy import GRID_SCENARIO, VolatilityGridStrategy  # noqa: E402

BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"

API = {
    "finder": "http://127.0.0.1:8080/api/v1",
    "strategy": "http://127.0.0.1:8081/api/v1",
    "grid": "http://127.0.0.1:8082/api/v1",
}

STRATEGY_MAP = {
    "TripleEmaStrategy": (TripleEmaStrategy, {
        "scenario_id": "trend_ema",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "TripleEmaStrategy",
        "label": "EMA 50/200",
    }),
    "AdxMomentumStrategy": (AdxMomentumStrategy, {
        "scenario_id": "trend_breakout",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "AdxMomentumStrategy",
        "label": "Breakout-Retest",
    }),
    "BollingerRsiStrategy": (BollingerRsiStrategy, {
        "scenario_id": "lite_mean_rev",
        "scan_type": "strategy",
        "group": "lite",
        "strategy": "BollingerRsiStrategy",
        "label": "Mean-reversion BB",
    }),
    "LiteIntradayStrategy": (LiteIntradayStrategy, {
        "scenario_id": "lite_intraday",
        "scan_type": "strategy",
        "group": "lite",
        "strategy": "LiteIntradayStrategy",
        "label": "Intraday",
    }),
    "LiteRangeStrategy": (LiteRangeStrategy, {
        "scenario_id": "lite_range",
        "scan_type": "strategy",
        "group": "lite",
        "strategy": "LiteRangeStrategy",
        "label": "Range",
    }),
}


def _auth() -> str:
    user = os.environ.get("FREQUI_USERNAME", "cryptotools")
    pw = os.environ.get("FREQUI_PASSWORD", "")
    env = Path(os.environ.get("CT_ENV", "/home/cryptotools/.cryptotools.env"))
    if env.is_file() and not pw:
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("FREQUI_PASSWORD="):
                pw = line.split("=", 1)[1].strip().strip('"').strip("'")
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def api_get(url: str) -> dict | list | None:
    req = urllib.request.Request(url, headers={"Authorization": _auth()}, method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def ft_pair_to_bybit(pair: str) -> str:
    base = pair.split("/")[0]
    return f"{base}USDT"


def fetch_klines(pair: str, limit: int = 220) -> pd.DataFrame:
    sym = ft_pair_to_bybit(pair)
    q = urllib.parse.urlencode(
        {"category": "linear", "symbol": sym, "interval": "5", "limit": str(limit)}
    )
    req = urllib.request.Request(f"{BYBIT_KLINE}?{q}", method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    rows = list(reversed(data.get("result", {}).get("list", [])))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(
        rows, columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"]
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms", utc=True)
    return df.dropna(subset=["close"])


def load_config(bot: str) -> dict:
    names = {"finder": "config.json", "strategy": "config_strategy.json", "grid": "config_grid.json"}
    return json.loads((USER_DATA / names[bot]).read_text(encoding="utf-8"))


def bot_capacity() -> list[dict]:
    rows = []
    for bot, base in API.items():
        try:
            cnt = api_get(f"{base}/count") or {}
            cfg = load_config(bot)
            rows.append(
                {
                    "bot": bot,
                    "open": int(cnt.get("current") or 0),
                    "max": int(cnt.get("max") or 0),
                    "free": max(0, int(cnt.get("max") or 0) - int(cnt.get("current") or 0)),
                    "state": (api_get(f"{base}/show_config") or {}).get("state", "?"),
                }
            )
        except Exception as exc:
            rows.append({"bot": bot, "error": str(exc)})
    return rows


def collect_pairs(limit: int) -> list[str]:
    pairs: list[str] = []
    seen: set[str] = set()
    for bot in API:
        try:
            wl = api_get(f"{API[bot]}/whitelist") or {}
            for p in wl.get("whitelist") or []:
                if p not in seen:
                    seen.add(p)
                    pairs.append(p)
        except Exception:
            pass
    return pairs[:limit]


def enabled_strategies() -> list[str]:
    path = USER_DATA / "enabled_strategies.json"
    if not path.is_file():
        return list(STRATEGY_MAP)
    data = json.loads(path.read_text(encoding="utf-8"))
    enabled = data.get("enabled") or {}
    return [sid for sid, on in enabled.items() if on and sid in STRATEGY_MAP]


def minimal_config() -> dict:
    return {
        "stake_amount": 10,
        "stoploss": -0.05,
        "minimal_roi": {"0": 0.03},
        "timeframe": "5m",
    }


def strategy_signals(df: pd.DataFrame, pair: str, strategy_ids: list[str]) -> list[dict]:
    out: list[dict] = []
    meta = {"pair": pair}
    cfg = minimal_config()
    for sid in strategy_ids:
        cls, scenario = STRATEGY_MAP[sid]
        try:
            inst = cls(cfg)
            frame = df.copy()
            frame = inst.populate_indicators(frame, meta)
            frame = inst.populate_entry_trend(frame, meta)
            last = frame.iloc[-1]
            if int(last.get("enter_long") or 0) == 1:
                out.append({"strategy": sid, "side": "long", "scenario": scenario, "tag": sid})
            if int(last.get("enter_short") or 0) == 1:
                out.append({"strategy": sid, "side": "short", "scenario": scenario, "tag": sid})
        except Exception as exc:
            out.append({"strategy": sid, "error": str(exc)})
    return out


def grid_signal(df: pd.DataFrame, pair: str) -> list[dict]:
    cfg = minimal_config()
    try:
        inst = VolatilityGridStrategy(cfg)
        frame = df.copy()
        frame = inst.populate_indicators(frame, {"pair": pair})
        frame = inst.populate_entry_trend(frame, {"pair": pair})
        last = frame.iloc[-1]
        hits = []
        if int(last.get("enter_long") or 0) == 1:
            hits.append({"strategy": "VolatilityGridStrategy", "side": "long", "scenario": GRID_SCENARIO})
        if int(last.get("enter_short") or 0) == 1:
            hits.append({"strategy": "VolatilityGridStrategy", "side": "short", "scenario": GRID_SCENARIO})
        return hits
    except Exception as exc:
        return [{"strategy": "VolatilityGridStrategy", "error": str(exc)}]


def eval_ml_gate(
    scenario: dict,
    pair: str,
    side: str,
    df: pd.DataFrame,
    *,
    stake: float,
    stoploss: float,
) -> dict:
    gate = get_live_ml_gate()
    rate = float(df["close"].iloc[-1])
    now = datetime.now(tz=UTC)
    market = features_from_ohlcv(df)
    row = gate._build_row(
        scenario=scenario,
        pair=pair,
        rate=rate,
        is_short=side == "short",
        current_time=now,
        stake_usdt=stake,
        stoploss=stoploss,
        minimal_roi={"0": 0.03},
        timeframe="5m",
        market=market,
    )
    ml = gate.predict_row(row)
    mode, min_conf = gate._resolve_gate_rules(scenario)
    blocked = gate.should_block(ml, scenario=scenario)
    cp = float(ml.get("confidence_profit") or 0)
    return {
        "predicted": ml.get("predicted"),
        "profit_pct": round(cp * 100, 1),
        "loss_pct": round(float(ml.get("confidence_loss") or 0) * 100, 1),
        "gate_mode": mode,
        "min_conf_pct": round(min_conf * 100, 0),
        "would_enter": not blocked and ml.get("ready", True),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe live ML entry points")
    ap.add_argument("--pairs", type=int, default=50, help="Max pairs to scan")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    caps = bot_capacity()
    pairs = collect_pairs(args.pairs)
    strat_ids = enabled_strategies()
    finder = FinderLive()
    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")

    results: dict = {
        "probed_at": now,
        "capacity": caps,
        "pairs_scanned": len(pairs),
        "finder_hits": [],
        "strategy_hits": [],
        "grid_hits": [],
        "near_misses": [],
    }

    print(f"=== Live entry probe · {now} ===")
    print("Capacity:")
    for c in caps:
        if "error" in c:
            print(f"  {c['bot']}: ERROR {c['error']}")
        else:
            print(f"  {c['bot']}: {c['open']}/{c['max']} open · {c['state']}")
    print(f"Scanning {len(pairs)} pairs · strategies: {', '.join(strat_ids)}")

    for pair in pairs:
        try:
            df = fetch_klines(pair)
        except Exception as exc:
            continue
        if len(df) < 60:
            continue

        # ML Finder
        sig = finder.scan_last_bar(pair, df)
        if sig:
            gate_ml = {}
            try:
                finder.allow_with_classifier_gate(sig, current_time=datetime.now(tz=UTC), ohlcv_df=df)
                gm = sig.get("gate_ml") or {}
                gate_ml = {
                    "profit_pct": round(float(gm.get("confidence_profit") or 0) * 100, 1),
                    "predicted": gm.get("predicted"),
                }
            except Exception:
                pass
            fm = sig.get("finder_ml") or {}
            row = {
                "pair": pair,
                "side": sig["side"],
                "finder_profit_pct": round(float(fm.get("confidence_profit") or 0) * 100, 1),
                "gate": gate_ml,
            }
            results["finder_hits"].append(row)
            print(
                f"  FINDER {pair} {sig['side']} finder={row['finder_profit_pct']}% "
                f"classifier={gate_ml.get('profit_pct', '?')}%"
            )

        # Strategy bot signals + ML gate
        for hit in strategy_signals(df, pair, strat_ids):
            if "error" in hit:
                continue
            ml = eval_ml_gate(
                hit["scenario"],
                pair,
                hit["side"],
                df,
                stake=10,
                stoploss=-0.05,
            )
            row = {**hit, "pair": pair, "ml": ml}
            if ml["would_enter"]:
                results["strategy_hits"].append(row)
                print(
                    f"  STRAT  {pair} {hit['strategy']} {hit['side']} "
                    f"profit={ml['profit_pct']}% (min {ml['min_conf_pct']}%) ALLOW"
                )
            elif ml["profit_pct"] >= ml["min_conf_pct"] * 0.7:
                results["near_misses"].append(row)
                print(
                    f"  near   {pair} {hit['strategy']} {hit['side']} "
                    f"profit={ml['profit_pct']}% pred={ml['predicted']} BLOCK"
                )

        for hit in grid_signal(df, pair):
            if "error" in hit:
                continue
            ml = eval_ml_gate(hit["scenario"], pair, hit["side"], df, stake=10, stoploss=-0.05)
            row = {**hit, "pair": pair, "ml": ml}
            if ml["would_enter"]:
                results["grid_hits"].append(row)
                print(
                    f"  GRID   {pair} {hit['side']} profit={ml['profit_pct']}% "
                    f"pred={ml['predicted']} ALLOW"
                )

    print("---")
    print(
        f"Finder: {len(results['finder_hits'])} · "
        f"Strategy (ML ok): {len(results['strategy_hits'])} · "
        f"Grid: {len(results['grid_hits'])} · "
        f"Near-miss: {len(results['near_misses'])}"
    )
    if not any(
        (
            results["finder_hits"],
            results["strategy_hits"],
            results["grid_hits"],
        )
    ):
        print("Сейчас нет точек входа, проходящих ML gate (Finder, стратегии и Grid ≥80% profit).")

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Backtest TA strategy waves; train ML before cut, test from cut onwards."""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SIM_SKIP_PERSIST", "1")

from simulation.exchange_sim.bot_session import BotSessionManager  # noqa: E402
from simulation.ml.pnl_classifier import (  # noqa: E402
    build_dataframe,
    evaluate_pipeline,
    feature_columns,
    make_market_store,
    make_pipeline,
)
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402

SCALP_SCENARIOS = [
    "scalp_ema",
    "scalp_rsi",
    "scalp_bb",
    "scalp_stoch",
    "scalp_macd",
    "scalp_liq_breakout",
    "scalp_liq_sweep",
]
NEWSET_SCENARIOS = [
    "new_keltner",
    "new_donchian",
    "new_cci",
    "new_ichimoku",
    "new_psar",
]
CHART_SCENARIOS = [
    "chart_willr",
    "chart_adxdi",
    "chart_squeeze",
    "chart_ha",
    "chart_pivot",
]
CHART2_SCENARIOS = [
    "chart2_aroon",
    "chart2_mfi",
    "chart2_stochrsi",
    "chart2_trix",
    "chart2_uo",
    "chart2_obv",
    "chart2_engulf",
    "chart2_vortex",
    "chart2_cmf",
    "chart2_kama",
]
CHART3_SCENARIOS = [
    "chart3_ao",
    "chart3_ppo",
    "chart3_cmo",
    "chart3_tema",
    "chart3_adosc",
    "chart3_roc",
    "chart3_elder",
    "chart3_hma",
    "chart3_fisher",
    "chart3_atrch",
]
COMBO_SCENARIOS = [
    "combo_ema_rsi_atr",
    "combo_adx_macd_vol",
    "combo_bb_rsi_adx",
    "combo_st_rsi_obv",
    "combo_don_adx_vol",
    "combo_stoch_cmf_ema",
    "combo_hma_ppo_atr",
    "combo_kc_stoch_vol",
]
CHART4_SCENARIOS = [
    "chart4_stc",
    "chart4_qqe",
    "chart4_dem",
    "chart4_kst",
    "chart4_rvi",
    "chart4_vwap",
    "chart4_rsidiv",
    "chart4_chandelier",
    "chart4_zscore",
    "chart4_ttm",
]

# Default cut = start of test window (overridden in main from --test-range).
# Train uses ALL available history before cut (data from 2025-01-01).
CUT_MS = int(datetime(2026, 4, 1, tzinfo=UTC).timestamp() * 1000)
DEFAULT_TIMERANGE = "20250101-20260625"
DEFAULT_OUT_DIR = ROOT / "simulation/results/scalp_compare"


def timerange_to_ms(tr: str) -> tuple[int, int]:
    start_s, end_s = tr.split("-", 1)
    s = datetime.strptime(start_s.strip(), "%Y%m%d").replace(tzinfo=UTC)
    e = datetime.strptime(end_s.strip(), "%Y%m%d").replace(hour=23, minute=59, second=59, tzinfo=UTC)
    return int(s.timestamp() * 1000), int(e.timestamp() * 1000)


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {
            "n": 0,
            "wins": 0,
            "losses": 0,
            "winrate": 0.0,
            "pnl": 0.0,
            "avg_pnl": 0.0,
        }
    wins = sum(1 for t in trades if float(t.get("profit_abs") or 0) >= 0)
    pnl = sum(float(t.get("profit_abs") or 0) for t in trades)
    n = len(trades)
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "winrate": round(wins / n, 4) if n else 0.0,
        "pnl": round(pnl, 4),
        "avg_pnl": round(pnl / n, 4) if n else 0.0,
    }


def apply_ml_gate(
    pipe: Any,
    test_df: Any,
    trades: list[dict[str, Any]],
    *,
    min_profit_proba: float = 0.55,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from sklearn.metrics import accuracy_score, roc_auc_score

    features = feature_columns()
    proba = pipe.predict_proba(test_df[features])
    classes = list(getattr(pipe.named_steps["clf"], "classes_", [0, 1]))
    profit_idx = classes.index(1) if 1 in classes else len(classes) - 1
    keep_mask = proba[:, profit_idx] >= min_profit_proba

    # Match back to trades by open_ms + pair (stable enough for this study).
    buckets: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for t in trades:
        oms = int((t.get("trade") or {}).get("open_ms") or t.get("open_ms") or 0)
        pair = str(t.get("pair") or "")
        buckets.setdefault((oms, pair), []).append(t)

    kept: list[dict[str, Any]] = []
    for row, keep in zip(test_df.itertuples(index=False), keep_mask, strict=False):
        if not keep:
            continue
        key = (int(row.open_ms), str(row.pair))
        bucket = buckets.get(key) or []
        if bucket:
            kept.append(bucket.pop(0))

    pred = (proba[:, profit_idx] >= 0.5).astype(int)
    y = test_df["label"].to_numpy()
    clf_metrics = {
        "kept": len(kept),
        "blocked": max(0, len(trades) - len(kept)),
        "keep_rate": round(len(kept) / len(trades), 4) if trades else 0.0,
        "accuracy": round(float(accuracy_score(y, pred)), 4),
        "roc_auc": (
            round(float(roc_auc_score(y, proba[:, profit_idx])), 4) if len(set(y)) > 1 else None
        ),
    }
    return kept, clf_metrics


def wrap_trade_record(tr: dict[str, Any], sid: str, pair: str, sc: dict[str, Any] | None = None) -> dict[str, Any]:
    """Shape flat normalized trades into trade_db-like records for ML features."""
    sc = sc or {}
    profit = float(tr.get("profit_abs") or 0)
    return {
        "id": f"{sid}:{pair}:{tr.get('open_ms')}:{int(bool(tr.get('is_short')))}",
        "scenario_id": sid,
        "pair": pair,
        "profit_abs": profit,
        "label": "profit" if profit >= 0 else "loss",
        "trade": {
            "open_ms": int(tr.get("open_ms") or 0),
            "close_ms": int(tr.get("close_ms") or 0),
            "open_rate": float(tr.get("open_rate") or 0),
            "close_rate": float(tr.get("close_rate") or 0),
            "is_short": bool(tr.get("is_short")),
            "profit_abs": profit,
            "exit_reason": tr.get("exit_reason") or "",
        },
        "basis": {
            "scenario_id": sid,
            "pair": pair,
            "strategy": sc.get("strategy") or sid,
            "scan_type": sc.get("scan_type") or "strategy",
            "group": sc.get("group") or "scalp",
            "stake_usdt": float(sc.get("stake_usdt") or 15),
            "stoploss": float(sc.get("stoploss") if sc.get("stoploss") is not None else tr.get("stop_loss") or 0),
            "minimal_roi": sc.get("minimal_roi") or {"0": 0.008},
            "timeframe": "5m",
        },
    }


def collect_scenario_trades(
    mgr: BotSessionManager,
    sid: str,
    pairs: list[str],
    start_ms: int,
    end_ms: int,
    datadir: Path,
    *,
    isolate_key: str | None = None,
) -> list[dict[str, Any]]:
    print(f"\n=== backtest {sid} · {len(pairs)} pairs ===", flush=True)
    mgr.load_scenario_instances(
        sid, pairs, start_ms, end_ms, datadir, isolate_key=isolate_key
    )
    sc = next((s for s in mgr.scenarios if s["id"] == sid), {}) or {}
    out: list[dict[str, Any]] = []
    for inst in mgr.instances:
        if (inst.get("scenario_id") or "") != sid:
            continue
        pair = inst.get("pair") or ""
        for tr in inst.get("raw_trades") or inst.get("scanner_trades") or inst.get("trades") or []:
            out.append(wrap_trade_record(tr, sid, pair, sc))
    print(f"  trades={len(out)}", flush=True)
    return out


def collect_train_test_trades(
    mgr: BotSessionManager,
    sid: str,
    pairs: list[str],
    datadir: Path,
    *,
    train_range: str,
    test_range: str,
    isolate_key: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate backtests so train capital lock doesn't wipe the OOS window."""
    tr_start, tr_end = timerange_to_ms(train_range)
    te_start, te_end = timerange_to_ms(test_range)
    print(f"\n=== {sid} TRAIN {train_range} ===", flush=True)
    train = collect_scenario_trades(
        mgr, sid, pairs, tr_start, tr_end, datadir, isolate_key=isolate_key
    )
    print(f"\n=== {sid} TEST {test_range} ===", flush=True)
    test = collect_scenario_trades(
        mgr, sid, pairs, te_start, te_end, datadir, isolate_key=isolate_key
    )
    return train, test


def evaluate_scenario(
    root: Path,
    sid: str,
    train_tr: list[dict[str, Any]],
    test_tr: list[dict[str, Any]],
    market_store: Any,
    *,
    cut_ms: int,
    min_trades_train: int = 40,
) -> dict[str, Any]:
    trades = train_tr + test_tr
    result: dict[str, Any] = {
        "scenario_id": sid,
        "n_all": len(trades),
        "n_train": len(train_tr),
        "n_test": len(test_tr),
        "all": summarize(trades),
        "train_raw": summarize(train_tr),
        "test_raw": summarize(test_tr),
    }
    if len(train_tr) < min_trades_train or len(test_tr) < 10:
        result["ml"] = {
            "skipped": True,
            "reason": f"need train>={min_trades_train} and test>=10 "
            f"(got {len(train_tr)}/{len(test_tr)})",
        }
        result["score"] = result["test_raw"]["pnl"]
        return result

    df = build_dataframe(trades, market_store)
    train_df = df[df["open_ms"] < cut_ms].copy()
    test_df = df[df["open_ms"] >= cut_ms].copy()
    # If calendar filter drifts, fall back to explicit lists' open_ms sets
    if len(test_df) < 10:
        train_ms = {int((t.get("trade") or {}).get("open_ms") or 0) for t in train_tr}
        test_ms = {int((t.get("trade") or {}).get("open_ms") or 0) for t in test_tr}
        train_df = df[df["open_ms"].isin(train_ms)].copy()
        test_df = df[df["open_ms"].isin(test_ms)].copy()
    if len(train_df) < min_trades_train or len(test_df) < 10:
        result["ml"] = {"skipped": True, "reason": "dataframe too small after features"}
        result["score"] = result["test_raw"]["pnl"]
        return result

    pipe = make_pipeline("lightgbm", calibrate=True)
    clf_metrics = evaluate_pipeline(pipe, train_df, test_df)
    kept, gate_meta = apply_ml_gate(pipe, test_df, test_tr, min_profit_proba=0.55)
    result["ml"] = {
        "skipped": False,
        "classifier": clf_metrics,
        "gate": gate_meta,
        "test_ml": summarize(kept),
    }
    result["score"] = result["ml"]["test_ml"]["pnl"]
    result["score_raw_test"] = result["test_raw"]["pnl"]
    return result


def resolve_scenarios(arg: str) -> list[str]:
    key = arg.strip().lower()
    if key == "newset":
        return list(NEWSET_SCENARIOS)
    if key in {"chart", "chartta", "ta"}:
        return list(CHART_SCENARIOS)
    if key in {"chart2", "wave2", "ta2"}:
        return list(CHART2_SCENARIOS)
    if key in {"chart3", "wave3", "ta3"}:
        return list(CHART3_SCENARIOS)
    if key in {"combo", "combos", "triad"}:
        return list(COMBO_SCENARIOS)
    if key in {"chart4", "wave4", "ta4"}:
        return list(CHART4_SCENARIOS)
    if key == "scalp":
        return list(SCALP_SCENARIOS)
    return [s.strip() for s in arg.split(",") if s.strip()]


def main() -> int:
    import argparse

    global CUT_MS

    ap = argparse.ArgumentParser(description="Compare TA strategies with calendar ML cut")
    ap.add_argument("--timerange", default=DEFAULT_TIMERANGE, help="unused if train/test set")
    # All history before April → test from April (local OHLCV starts 2025-01-01).
    ap.add_argument("--train-range", default="20250101-20260331")
    ap.add_argument("--test-range", default="20260401-20260625")
    ap.add_argument("--pairs", default="pool", help="pool|prod200|scalp")
    ap.add_argument("--max-pairs", type=int, default=20)
    ap.add_argument(
        "--scenarios",
        default=",".join(SCALP_SCENARIOS),
        help="Comma-separated ids, or alias: scalp|newset|chart|chart2|chart3|chart4|combo",
    )
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = ap.parse_args()

    scenarios = resolve_scenarios(args.scenarios)
    te_start, _ = timerange_to_ms(args.test_range)
    CUT_MS = te_start
    cut_date = datetime.fromtimestamp(CUT_MS / 1000, tz=UTC).strftime("%Y-%m-%d")

    if args.pairs == "scalp":
        path = ROOT / "simulation/config/scalp_pairs_whitelist.json"
        pairs = list(json.loads(path.read_text(encoding="utf-8")).get("pairs") or [])
    else:
        pairs = pairs_from_source(ROOT, args.pairs, min_start="2025-01-01")
    pairs = pairs[: args.max_pairs]
    datadir = ROOT / "simulation/data/ctengine"
    out_dir = Path(args.out_dir)

    print(
        f"=== STRATEGY COMPARE · {len(scenarios)} bots · {len(pairs)} pairs · "
        f"train={args.train_range} test={args.test_range} · cut={cut_date} ===",
        flush=True,
    )
    print(f"  pairs: {pairs}", flush=True)

    mgr = BotSessionManager(ROOT)
    try:
        mgr._get_ml_gate().set_enabled(False)
    except Exception:
        pass
    mgr.scan_schedule = {"rolling_whitelist": False}
    pairs = [p for p in pairs if not p.startswith("XRP/")]

    by_sid: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for sid in scenarios:
        by_sid[sid] = collect_train_test_trades(
            mgr,
            sid,
            pairs,
            datadir,
            train_range=args.train_range,
            test_range=args.test_range,
        )

    market_store = make_market_store(ROOT)
    rows: list[dict[str, Any]] = []
    for sid in scenarios:
        print(f"\n=== evaluate {sid} ===", flush=True)
        train_tr, test_tr = by_sid[sid]
        rows.append(
            evaluate_scenario(ROOT, sid, train_tr, test_tr, market_store, cut_ms=CUT_MS)
        )

    ranked = sorted(rows, key=lambda r: float(r.get("score") or -1e18), reverse=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "train_range": args.train_range,
        "test_range": args.test_range,
        "cut_ms": CUT_MS,
        "cut_date": cut_date,
        "train": f"separate backtest {args.train_range}",
        "test": f"separate backtest {args.test_range}",
        "pairs": pairs,
        "scenarios": rows,
        "ranking": [
            {
                "rank": i + 1,
                "scenario_id": r["scenario_id"],
                "score_ml_test_pnl": r.get("score"),
                "raw_test_pnl": r.get("test_raw", {}).get("pnl"),
                "test_n": r.get("n_test"),
                "ml": r.get("ml"),
            }
            for i, r in enumerate(ranked)
        ],
        "winner": ranked[0]["scenario_id"] if ranked else None,
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }
    out_path = out_dir / "report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n========== RANKING (ML-gated test PnL) ==========", flush=True)
    for i, r in enumerate(ranked, 1):
        ml = r.get("ml") or {}
        test_ml = (ml.get("test_ml") or {}) if not ml.get("skipped") else {}
        print(
            f"{i}. {r['scenario_id']}: raw_test={r['test_raw']['pnl']} "
            f"({r['n_test']} tr, wr={r['test_raw']['winrate']}) | "
            f"ml_test={test_ml.get('pnl', 'skip')} "
            f"kept={test_ml.get('n', '-')} | score={r.get('score')}",
            flush=True,
        )
    print(f"\nWinner: {report['winner']}", flush=True)
    print(f"Saved: {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

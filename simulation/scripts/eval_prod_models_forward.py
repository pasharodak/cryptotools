#!/usr/bin/env python3
"""Score frozen production models on a forward window (no retrain).

Default: live test-block models in site/user_data/models, August–September 2026.
Trades come from a fresh backtest; the joblib and its min_profit_proba stay as shipped.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import BotSessionManager  # noqa: E402
from simulation.ml.pnl_classifier import (  # noqa: E402
    activate_feature_set,
    build_dataframe,
    make_market_store,
)
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402
from simulation.scripts.run_scalp_strategies_compare import (  # noqa: E402
    apply_ml_gate,
    collect_scenario_trades,
    summarize,
    timerange_to_ms,
)

PACK_PATH = ROOT / "simulation/config/prod_top30_pack.json"
MODEL_ROOT = ROOT / "site/user_data/models/pnl_classifier/by_scenario"
OUT_DIR = ROOT / "simulation/results/prod_forward_augsep"
DEFAULT_RANGE = "20260801-20260927"


def _load_pack() -> list[dict[str, Any]]:
    data = json.loads(PACK_PATH.read_text(encoding="utf-8"))
    return list(data.get("strategies") or [])


def _prod_rows(pack: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [s for s in pack if s.get("test_group") and s.get("scenario_id")]
    return sorted(rows, key=lambda s: int(s.get("num") or 0))


def _month(rec: dict[str, Any]) -> str:
    oms = int((rec.get("trade") or {}).get("open_ms") or 0)
    if not oms:
        return ""
    return datetime.fromtimestamp(oms / 1000, tz=UTC).strftime("%Y-%m")


def _by_month(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        buckets.setdefault(_month(t), []).append(t)
    return {m: summarize(rows) for m, rows in sorted(buckets.items()) if m}


def _trained_constants(pipe: Any) -> dict[str, str]:
    """Single-valued categoricals the model was fit on. Unknown labels zero the one-hot."""
    pre = pipe.named_steps.get("pre")
    ct = getattr(pre, "preprocessor", pre)
    out: dict[str, str] = {}
    if ct is None:
        return out
    for name, trans, cols in getattr(ct, "transformers_", []):
        if name != "cat":
            continue
        cats = getattr(trans, "categories_", None)
        if cats is None and hasattr(trans, "named_steps"):
            for step in trans.named_steps.values():
                if hasattr(step, "categories_"):
                    cats = step.categories_
                    break
        if cats is None:
            continue
        for col, cat in zip(list(cols), cats):
            if str(col) == "is_short" or len(cat) != 1:
                continue
            out[str(col)] = str(cat[0])
    return out


def _align_trades(trades: list[dict[str, Any]], constants: dict[str, str]) -> list[dict[str, Any]]:
    aligned: list[dict[str, Any]] = []
    for rec in trades:
        row = dict(rec)
        basis = dict(row.get("basis") or {})
        if "scenario_id" in constants:
            row["scenario_id"] = constants["scenario_id"]
            basis["scenario_id"] = constants["scenario_id"]
        for key in ("group", "strategy", "scan_type"):
            if key in constants:
                basis[key] = constants[key]
        row["basis"] = basis
        aligned.append(row)
    return aligned


def _score_one(
    sid: str,
    trades: list[dict[str, Any]],
    store: Any,
    *,
    gate: float,
    feature_set: str,
) -> dict[str, Any]:
    activate_feature_set(feature_set or "core")
    pipe = joblib.load(MODEL_ROOT / sid / "pnl_classifier.joblib")
    trades = _align_trades(trades, _trained_constants(pipe))
    df = build_dataframe(trades, store)
    kept, clf = apply_ml_gate(pipe, df, trades, min_profit_proba=gate)
    return {
        "gate": gate,
        "feature_set": feature_set or "core",
        "classifier": clf,
        "raw": summarize(trades),
        "ml": summarize(kept),
        "raw_by_month": _by_month(trades),
        "ml_by_month": _by_month(kept),
    }


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Forward-test frozen prod models")
    ap.add_argument("--timerange", default=DEFAULT_RANGE)
    ap.add_argument("--pairs", default="prod200")
    ap.add_argument("--scenarios", default="test", help="test | comma-separated scenario ids")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--skip-collect", action="store_true")
    args = ap.parse_args()

    pack = _load_pack()
    if args.scenarios.strip().lower() == "test":
        rows = _prod_rows(pack)
    else:
        want = {s.strip() for s in args.scenarios.split(",") if s.strip()}
        rows = [s for s in pack if s.get("scenario_id") in want]
    if not rows:
        print("no scenarios", flush=True)
        return 1

    pairs = pairs_from_source(ROOT, args.pairs)
    start_ms, end_ms = timerange_to_ms(args.timerange)
    out_dir = Path(args.out_dir)
    cache_dir = out_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"=== prod forward · {len(rows)} models · {len(pairs)} pairs · {args.timerange} ===",
        flush=True,
    )

    datadir = ROOT / "simulation/data/ctengine"
    mgr = BotSessionManager(ROOT)
    try:
        mgr._get_ml_gate().set_enabled(False)
    except Exception:
        pass
    mgr.scan_schedule = {"rolling_whitelist": False}

    collected: dict[str, list[dict[str, Any]]] = {}
    for i, row in enumerate(rows, 1):
        sid = row["scenario_id"]
        cache = cache_dir / f"{sid}.json"
        if args.skip_collect and cache.is_file():
            trades = json.loads(cache.read_text(encoding="utf-8"))
            print(f"[{i}/{len(rows)}] cache {sid}: {len(trades)}", flush=True)
        elif cache.is_file() and not args.skip_collect:
            trades = json.loads(cache.read_text(encoding="utf-8"))
            print(f"[{i}/{len(rows)}] cache hit {sid}: {len(trades)}", flush=True)
        else:
            print(f"\n[{i}/{len(rows)}] backtest {sid}", flush=True)
            trades = collect_scenario_trades(
                mgr,
                sid,
                pairs,
                start_ms,
                end_ms,
                datadir,
                isolate_key=f"prod_fwd_{sid}",
            )
            cache.write_text(json.dumps(trades, ensure_ascii=False), encoding="utf-8")
        collected[sid] = trades

    store = make_market_store(ROOT)
    results: list[dict[str, Any]] = []
    for row in rows:
        sid = row["scenario_id"]
        trades = collected[sid]
        meta_path = MODEL_ROOT / sid / "pnl_classifier_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        gate = float(meta.get("min_profit_proba") if meta.get("min_profit_proba") is not None else row.get("min_profit_proba") or 0.45)
        feature_set = str(meta.get("feature_set") or "core")
        print(f"\n=== score {sid} n={len(trades)} gate={gate} set={feature_set} ===", flush=True)
        if len(trades) < 5 or not (MODEL_ROOT / sid / "pnl_classifier.joblib").is_file():
            item = {
                "scenario_id": sid,
                "num": row.get("num"),
                "label": row.get("label"),
                "skipped": True,
                "n": len(trades),
                "reason": "too few trades or model missing",
            }
            results.append(item)
            print(f"  skip {item['reason']}", flush=True)
            continue
        try:
            scored = _score_one(sid, trades, store, gate=gate, feature_set=feature_set)
        except Exception as exc:
            results.append(
                {
                    "scenario_id": sid,
                    "num": row.get("num"),
                    "label": row.get("label"),
                    "skipped": True,
                    "n": len(trades),
                    "reason": str(exc),
                }
            )
            print(f"  FAILED {exc}", flush=True)
            continue
        item = {
            "scenario_id": sid,
            "num": row.get("num"),
            "label": row.get("label"),
            "model": meta.get("model"),
            "trained_at": meta.get("trained_at"),
            "train_range": meta.get("train_range"),
            "model_test_range": meta.get("test_range"),
            "stake_usdt": row.get("stake_usdt"),
            "skipped": False,
            **scored,
        }
        results.append(item)
        raw, ml = scored["raw"], scored["ml"]
        print(
            f"  raw n={raw['n']} pnl={raw['pnl']} wr={raw['winrate']} | "
            f"ml n={ml['n']} pnl={ml['pnl']} wr={ml['winrate']} "
            f"auc={scored['classifier'].get('roc_auc')}",
            flush=True,
        )

    def _sum(key: str) -> dict[str, float]:
        pnl = 0.0
        n = 0
        wins = 0
        for r in results:
            block = r.get(key) or {}
            pnl += float(block.get("pnl") or 0)
            n += int(block.get("n") or 0)
            wins += int(block.get("wins") or 0)
        return {
            "n": n,
            "wins": wins,
            "pnl": round(pnl, 4),
            "winrate": round(wins / n, 4) if n else 0.0,
        }

    report = {
        "timerange": args.timerange,
        "pairs_source": args.pairs,
        "n_pairs": len(pairs),
        "model_root": str(MODEL_ROOT),
        "note": (
            "Frozen prod test-block models. Gate is min_profit_proba from the shipped meta. "
            "PnL is the sum of independent scenario backtests at each scenario stake, not a shared wallet."
        ),
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "totals": {"raw": _sum("raw"), "ml": _sum("ml")},
        "scenarios": results,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n========== PROD FORWARD ==========", flush=True)
    print(f"raw {report['totals']['raw']}", flush=True)
    print(f"ml  {report['totals']['ml']}", flush=True)
    print(f"Saved: {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

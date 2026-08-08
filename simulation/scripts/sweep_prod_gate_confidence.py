#!/usr/bin/env python3
"""Sweep ML gate confidence thresholds on prod strategies (April cut).

Train: before April. Test: from April.
For each prod strategy: fit winner_exp on train only, then keep test trades
with profit_proba >= threshold for thresholds 60..95%.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.pnl_classifier import (  # noqa: E402
    evaluate_pipeline,
    feature_columns,
    make_market_store,
)
from simulation.scripts.run_ml_param_experiments import (  # noqa: E402
    all_experiments,
    build_split_frames,
    cache_path,
    load_cached,
    make_exp_pipeline,
    timerange_to_ms,
)
from simulation.scripts.run_scalp_strategies_compare import (  # noqa: E402
    apply_ml_gate,
    summarize,
)

PACK = ROOT / "simulation/config/prod_top30_pack.json"
CACHE = ROOT / "simulation/results/ml_param_experiments/trade_cache"
OUT_DIR = ROOT / "simulation/results/prod_gate_confidence_sweep"
THRESHOLDS = (0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)


def exp_by_id(exp_id: str) -> dict[str, Any]:
    for e in all_experiments():
        if e["id"] == exp_id:
            return e
    raise KeyError(f"unknown experiment: {exp_id}")


def pick_best(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    scored = [r for r in rows if r.get("pnl") is not None]
    if not scored:
        return None
    # Prefer higher PnL; ties → higher winrate, then more trades.
    return max(
        scored,
        key=lambda r: (float(r["pnl"]), float(r.get("winrate") or 0), int(r.get("n") or 0)),
    )


def sweep_one(
    *,
    sid: str,
    winner_exp: str,
    prod_gate: float,
    cut_ms: int,
    store: Any,
) -> dict[str, Any]:
    train_tr = load_cached(cache_path(CACHE, sid, "train")) or []
    test_tr = load_cached(cache_path(CACHE, sid, "test")) or []
    if len(train_tr) < 40 or len(test_tr) < 10:
        return {
            "scenario_id": sid,
            "skipped": True,
            "reason": f"need train>=40 test>=10 (got {len(train_tr)}/{len(test_tr)})",
            "n_train": len(train_tr),
            "n_test": len(test_tr),
        }

    train_df, test_df = build_split_frames(train_tr, test_tr, store, cut_ms)
    if len(train_df) < 40 or len(test_df) < 10:
        return {
            "scenario_id": sid,
            "skipped": True,
            "reason": "dataframe too small after features",
            "n_train": len(train_tr),
            "n_test": len(test_tr),
        }

    spec = exp_by_id(winner_exp)
    pipe = make_exp_pipeline(spec)
    clf_metrics = evaluate_pipeline(pipe, train_df, test_df)

    raw = summarize(test_tr)
    features = feature_columns()
    proba = pipe.predict_proba(test_df[features])
    classes = list(getattr(pipe.named_steps["clf"], "classes_", [0, 1]))
    profit_idx = classes.index(1) if 1 in classes else len(classes) - 1
    profit_proba = [float(x) for x in proba[:, profit_idx]]

    # Reference: current prod gate + requested sweep.
    gates = [float(prod_gate), *THRESHOLDS]
    # unique, preserve order
    seen: set[float] = set()
    ordered_gates: list[float] = []
    for g in gates:
        key = round(g, 4)
        if key in seen:
            continue
        seen.add(key)
        ordered_gates.append(float(g))

    by_gate: list[dict[str, Any]] = []
    for th in ordered_gates:
        kept, gate_meta = apply_ml_gate(pipe, test_df, test_tr, min_profit_proba=th)
        ml = summarize(kept)
        by_gate.append(
            {
                "min_profit_proba": th,
                "is_prod_gate": abs(th - float(prod_gate)) < 1e-9,
                "in_sweep": any(abs(th - t) < 1e-9 for t in THRESHOLDS),
                "n": ml["n"],
                "wins": ml["wins"],
                "losses": ml["losses"],
                "winrate": ml["winrate"],
                "pnl": ml["pnl"],
                "avg_pnl": ml["avg_pnl"],
                "keep_rate": gate_meta.get("keep_rate"),
                "blocked": gate_meta.get("blocked"),
            }
        )

    sweep_only = [r for r in by_gate if r.get("in_sweep")]
    best = pick_best(sweep_only)
    prod_row = next((r for r in by_gate if r.get("is_prod_gate")), None)

    return {
        "scenario_id": sid,
        "skipped": False,
        "winner_exp": winner_exp,
        "prod_gate": float(prod_gate),
        "n_train": len(train_tr),
        "n_test": len(test_tr),
        "classifier": clf_metrics,
        "test_raw": raw,
        "profit_proba_stats": {
            "avg": round(sum(profit_proba) / len(profit_proba), 4) if profit_proba else 0.0,
            "p50": round(sorted(profit_proba)[len(profit_proba) // 2], 4) if profit_proba else 0.0,
            "p90": round(sorted(profit_proba)[int(len(profit_proba) * 0.9)], 4) if profit_proba else 0.0,
            "max": round(max(profit_proba), 4) if profit_proba else 0.0,
            "ge_60": sum(1 for p in profit_proba if p >= 0.60),
            "ge_80": sum(1 for p in profit_proba if p >= 0.80),
            "ge_90": sum(1 for p in profit_proba if p >= 0.90),
            "ge_95": sum(1 for p in profit_proba if p >= 0.95),
        },
        "by_gate": by_gate,
        "best_sweep": best,
        "vs_prod": (
            None
            if not best or not prod_row
            else {
                "best_gate": best["min_profit_proba"],
                "best_pnl": best["pnl"],
                "prod_pnl": prod_row["pnl"],
                "delta_pnl": round(float(best["pnl"]) - float(prod_row["pnl"]), 4),
            }
        ),
    }


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Sweep prod ML gate confidence thresholds")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--max-strategies", type=int, default=0, help="0 = all")
    args = ap.parse_args()

    pack = json.loads(PACK.read_text(encoding="utf-8"))
    strategies = list(pack.get("strategies") or [])
    if args.max_strategies and args.max_strategies > 0:
        strategies = strategies[: args.max_strategies]

    train_range = pack.get("train_range") or "20250101-20260331"
    test_range = pack.get("test_range") or "20260401-20260625"
    cut_ms, _ = timerange_to_ms(test_range)
    cut_date = datetime.fromtimestamp(cut_ms / 1000, tz=UTC).strftime("%Y-%m-%d")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    store = make_market_store(ROOT)

    print(
        f"=== PROD GATE CONFIDENCE SWEEP · {len(strategies)} strategies · "
        f"train={train_range} test={test_range} cut={cut_date} · "
        f"thresholds={[int(t * 100) for t in THRESHOLDS]} ===",
        flush=True,
    )

    rows: list[dict[str, Any]] = []
    for i, s in enumerate(strategies, 1):
        sid = str(s["scenario_id"])
        winner = str(s.get("winner_exp") or "exp14_lgbm_gate045")
        prod_gate = float(s.get("min_profit_proba") or 0.55)
        label = s.get("label") or sid
        print(f"\n[{i}/{len(strategies)}] {sid} · {label} · exp={winner} · prod_gate={prod_gate}", flush=True)
        try:
            row = sweep_one(
                sid=sid,
                winner_exp=winner,
                prod_gate=prod_gate,
                cut_ms=cut_ms,
                store=store,
            )
        except Exception as exc:
            print(f"  FAILED: {exc}", flush=True)
            row = {
                "scenario_id": sid,
                "label": label,
                "skipped": True,
                "error": str(exc),
                "winner_exp": winner,
                "prod_gate": prod_gate,
            }
        row["label"] = label
        row["class_name"] = s.get("class_name")
        row["rank"] = s.get("rank")
        rows.append(row)

        if row.get("skipped"):
            print(f"  skip: {row.get('reason') or row.get('error')}", flush=True)
            continue
        best = row.get("best_sweep") or {}
        raw = row.get("test_raw") or {}
        print(
            f"  raw test: n={raw.get('n')} pnl={raw.get('pnl')} wr={raw.get('winrate')}",
            flush=True,
        )
        for g in row.get("by_gate") or []:
            if not g.get("in_sweep") and not g.get("is_prod_gate"):
                continue
            tag = "PROD" if g.get("is_prod_gate") else f"{int(round(g['min_profit_proba'] * 100))}%"
            print(
                f"  {tag:>5}: n={g['n']:4d} pnl={g['pnl']:+8.2f} wr={g['winrate']*100:5.1f}% "
                f"keep={g.get('keep_rate')}",
                flush=True,
            )
        if best:
            print(
                f"  BEST sweep: {int(round(best['min_profit_proba'] * 100))}% "
                f"pnl={best['pnl']:+.2f} n={best['n']}",
                flush=True,
            )

    ok = [r for r in rows if not r.get("skipped")]
    best_summary = []
    for r in ok:
        best = r.get("best_sweep") or {}
        vs = r.get("vs_prod") or {}
        best_summary.append(
            {
                "scenario_id": r["scenario_id"],
                "label": r.get("label"),
                "class_name": r.get("class_name"),
                "winner_exp": r.get("winner_exp"),
                "prod_gate": r.get("prod_gate"),
                "best_gate": best.get("min_profit_proba"),
                "best_pnl": best.get("pnl"),
                "best_n": best.get("n"),
                "best_winrate": best.get("winrate"),
                "prod_pnl": vs.get("prod_pnl"),
                "delta_vs_prod": vs.get("delta_pnl"),
                "raw_pnl": (r.get("test_raw") or {}).get("pnl"),
                "raw_n": (r.get("test_raw") or {}).get("n"),
            }
        )

    ranked = sorted(best_summary, key=lambda x: float(x.get("best_pnl") or -1e18), reverse=True)
    improved = [x for x in best_summary if (x.get("delta_vs_prod") or 0) > 0.01]
    worsened = [x for x in best_summary if (x.get("delta_vs_prod") or 0) < -0.01]

    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "train_range": train_range,
        "test_range": test_range,
        "cut_date": cut_date,
        "thresholds": list(THRESHOLDS),
        "methodology": (
            "Per prod strategy: train winner_exp classifier on trades before April cut only; "
            "on April+ test trades keep those with ML profit_proba >= threshold. "
            "Best = max test PnL among 60/65/70/75/80/85/90/95%."
        ),
        "n_strategies": len(strategies),
        "n_ok": len(ok),
        "n_skipped": len(rows) - len(ok),
        "strategies": rows,
        "best_per_strategy": best_summary,
        "ranking_by_best_pnl": [
            {
                "rank": i + 1,
                "scenario_id": r["scenario_id"],
                "label": r.get("label"),
                "best_gate": r.get("best_gate"),
                "best_pnl": r.get("best_pnl"),
                "best_n": r.get("best_n"),
                "prod_gate": r.get("prod_gate"),
                "delta_vs_prod": r.get("delta_vs_prod"),
            }
            for i, r in enumerate(ranked)
        ],
        "improved_vs_prod_count": len(improved),
        "worsened_vs_prod_count": len(worsened),
    }

    out_path = out_dir / "report.json"
    summary_path = out_dir / "summary.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    summary_path.write_text(
        json.dumps(
            {
                "generated_at": report["generated_at"],
                "train_range": train_range,
                "test_range": test_range,
                "thresholds": list(THRESHOLDS),
                "best_per_strategy": best_summary,
                "ranking_by_best_pnl": report["ranking_by_best_pnl"],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("\n========== BEST GATE PER STRATEGY (sweep 60–95%) ==========", flush=True)
    for i, r in enumerate(ranked, 1):
        bg = r.get("best_gate")
        bg_s = f"{int(round(bg * 100))}%" if bg is not None else "?"
        print(
            f"{i:2d}. {r['scenario_id']:22} best={bg_s:>4} pnl={r.get('best_pnl'):+8.2f} "
            f"n={r.get('best_n'):4}  prod={r.get('prod_gate')} "
            f"dVsProd={r.get('delta_vs_prod'):+7.2f}",
            flush=True,
        )
    print(f"\nSaved: {out_path}", flush=True)
    print(f"Summary: {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

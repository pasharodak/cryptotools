#!/usr/bin/env python3
"""Compare ML gates on SL/TP trade_cache cells:

  - raw (no ML)
  - global (one model on all pairs in the cell)
  - per_pair (model per pair; fallback to global if too few train trades)

Train/test stay strictly separated via cached __train.json / __test.json
(and open_ms cut). One LightGBM gate spec per run (default exp14).

Writes:
  simulation/results/ml_param_experiments/sltp_global_vs_pair/
    cells/{key}.json
    summary.json
    comparison.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.pnl_classifier import evaluate_pipeline, make_market_store  # noqa: E402
from simulation.scripts.run_ml_param_experiments import (  # noqa: E402
    DEFAULT_TEST,
    DEFAULT_TRAIN,
    all_experiments,
    build_split_frames,
    load_cached,
    make_exp_pipeline,
    timerange_to_ms,
)
from simulation.scripts.run_ml_sltp_grid import discover_cells  # noqa: E402
from simulation.scripts.run_scalp_strategies_compare import (  # noqa: E402
    apply_ml_gate,
    summarize,
)

DEFAULT_CACHE = ROOT / "simulation/results/ml_param_experiments/trade_cache_sltp_fee001"
DEFAULT_OUT = ROOT / "simulation/results/ml_param_experiments/sltp_global_vs_pair"
DEFAULT_EXP = "exp14_lgbm_gate045"


def rec_pair(rec: dict[str, Any]) -> str:
    return str(rec.get("pair") or (rec.get("basis") or {}).get("pair") or "")


def pair_safe(pair: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", pair)


def resolve_exp(exp_id: str) -> dict[str, Any]:
    by_id = {e["id"]: e for e in all_experiments()}
    if exp_id not in by_id:
        raise SystemExit(f"Unknown experiment id: {exp_id}")
    return by_id[exp_id]


def fit_and_gate(
    spec: dict[str, Any],
    train_tr: list[dict[str, Any]],
    test_tr: list[dict[str, Any]],
    train_df: Any,
    test_df: Any,
) -> dict[str, Any]:
    if len(train_df) < 20 or len(test_df) < 5:
        return {
            "skipped": True,
            "reason": f"too few rows train_df={len(train_df)} test_df={len(test_df)}",
            "test_ml": summarize([]),
            "score": None,
        }
    pipe = make_exp_pipeline(spec)
    clf = evaluate_pipeline(pipe, train_df, test_df)
    kept, gate = apply_ml_gate(
        pipe,
        test_df,
        test_tr,
        min_profit_proba=float(spec["min_profit_proba"]),
    )
    ml = summarize(kept)
    return {
        "skipped": False,
        "classifier": clf,
        "gate": gate,
        "test_ml": ml,
        "score": ml["pnl"],
        "kept": kept,
        "pipe": pipe,
    }


def run_cell(
    *,
    cell: dict[str, Any],
    spec: dict[str, Any],
    store: Any,
    cut_ms: int,
    min_pair_train: int,
    min_pair_test: int,
    force: bool,
    per_dir: Path,
) -> dict[str, Any]:
    out_path = per_dir / f"{cell['key']}.json"
    if out_path.is_file() and not force:
        prev = json.loads(out_path.read_text(encoding="utf-8"))
        if prev.get("global") is not None and prev.get("per_pair") is not None:
            prev["skipped_existing"] = True
            return prev

    train_tr = load_cached(Path(cell["train_path"])) or []
    test_tr = load_cached(Path(cell["test_path"])) or []
    raw = summarize(test_tr)
    base = {
        "scenario_id": cell["scenario_id"],
        "tp_pct": cell["tp_pct"],
        "sl_pct": cell["sl_pct"],
        "key": cell["key"],
        "n_train": len(train_tr),
        "n_test": len(test_tr),
        "test_raw": raw,
        "exp_id": spec["id"],
        "min_profit_proba": spec["min_profit_proba"],
    }
    if len(train_tr) < 40 or len(test_tr) < 10:
        out = {
            **base,
            "error": f"too few trades train={len(train_tr)} test={len(test_tr)}",
            "global": None,
            "per_pair": None,
            "winner_mode": "raw",
            "generated_at": datetime.now(tz=UTC).isoformat(),
        }
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    train_df, test_df = build_split_frames(train_tr, test_tr, store, cut_ms)

    # --- global ---
    g = fit_and_gate(spec, train_tr, test_tr, train_df, test_df)
    global_pipe = g.pop("pipe", None)
    g.pop("kept", None)
    global_row = dict(g)

    # --- per pair ---
    by_train: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_test: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in train_tr:
        p = rec_pair(rec)
        if p:
            by_train[p].append(rec)
    for rec in test_tr:
        p = rec_pair(rec)
        if p:
            by_test[p].append(rec)

    kept_all: list[dict[str, Any]] = []
    pair_rows: list[dict[str, Any]] = []
    n_pair_models = 0
    n_fallback_global = 0
    n_skipped_pairs = 0

    for pair in sorted(set(by_train) | set(by_test)):
        ptr = by_train.get(pair) or []
        pte = by_test.get(pair) or []
        if not pte:
            continue
        train_mask = train_df["pair"].astype(str) == pair
        test_mask = test_df["pair"].astype(str) == pair
        pdf_tr = train_df.loc[train_mask].copy()
        pdf_te = test_df.loc[test_mask].copy()

        used = "skipped"
        kept: list[dict[str, Any]] = []
        detail: dict[str, Any] = {}

        if len(ptr) >= min_pair_train and len(pdf_tr) >= min_pair_train and len(pdf_te) >= min_pair_test:
            try:
                local = fit_and_gate(spec, ptr, pte, pdf_tr, pdf_te)
                local.pop("pipe", None)
                if not local.get("skipped"):
                    kept = list(local.get("kept") or [])
                    detail = {
                        "classifier": local.get("classifier"),
                        "gate": local.get("gate"),
                        "test_ml": local.get("test_ml"),
                    }
                    used = "per_pair"
                    n_pair_models += 1
                else:
                    used = "skipped_local"
            except Exception as exc:
                detail = {"error": str(exc)}
                used = "error"

        if used != "per_pair":
            if global_pipe is not None and len(pdf_te) >= 1:
                try:
                    kept, gate = apply_ml_gate(
                        global_pipe,
                        pdf_te,
                        pte,
                        min_profit_proba=float(spec["min_profit_proba"]),
                    )
                    detail = {
                        "gate": gate,
                        "test_ml": summarize(kept),
                        "fallback": True,
                    }
                    used = "global_fallback"
                    n_fallback_global += 1
                except Exception as exc:
                    detail = {"error": str(exc)}
                    used = "error"
                    n_skipped_pairs += 1
            else:
                n_skipped_pairs += 1
                used = "skipped"

        kept_all.extend(kept)
        pair_rows.append(
            {
                "pair": pair,
                "n_train": len(ptr),
                "n_test": len(pte),
                "mode": used,
                "test_raw": summarize(pte),
                **{k: v for k, v in detail.items() if k != "kept"},
            }
        )

    per_pair_ml = summarize(kept_all)
    modes = {
        "raw": {"pnl": raw.get("pnl"), "n": raw.get("n"), "winrate": raw.get("winrate")},
        "global": {
            "pnl": (global_row.get("test_ml") or {}).get("pnl"),
            "n": (global_row.get("test_ml") or {}).get("n"),
            "winrate": (global_row.get("test_ml") or {}).get("winrate"),
            "skipped": global_row.get("skipped"),
        },
        "per_pair": {
            "pnl": per_pair_ml.get("pnl"),
            "n": per_pair_ml.get("n"),
            "winrate": per_pair_ml.get("winrate"),
            "n_pair_models": n_pair_models,
            "n_fallback_global": n_fallback_global,
            "n_skipped_pairs": n_skipped_pairs,
        },
    }
    scored = {
        k: float(v["pnl"])
        for k, v in modes.items()
        if v.get("pnl") is not None
    }
    winner_mode = max(scored, key=scored.get) if scored else "raw"

    out = {
        **base,
        "global": global_row,
        "per_pair": {
            "test_ml": per_pair_ml,
            "n_pair_models": n_pair_models,
            "n_fallback_global": n_fallback_global,
            "n_skipped_pairs": n_skipped_pairs,
            "pairs": pair_rows,
        },
        "modes": modes,
        "winner_mode": winner_mode,
        "delta_global_minus_raw": round(
            float(modes["global"]["pnl"] or 0) - float(modes["raw"]["pnl"] or 0), 4
        ),
        "delta_per_pair_minus_raw": round(
            float(modes["per_pair"]["pnl"] or 0) - float(modes["raw"]["pnl"] or 0), 4
        ),
        "delta_per_pair_minus_global": round(
            float(modes["per_pair"]["pnl"] or 0) - float(modes["global"]["pnl"] or 0), 4
        ),
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }
    # Drop huge per-pair detail from summary path? keep full in cell file.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def build_summary(rows: list[dict[str, Any]], *, exp_id: str) -> dict[str, Any]:
    ok = [r for r in rows if r.get("modes")]
    by_mode_sum = {"raw": 0.0, "global": 0.0, "per_pair": 0.0}
    win_counts = defaultdict(int)
    for r in ok:
        m = r["modes"]
        for mode in by_mode_sum:
            if m.get(mode, {}).get("pnl") is not None:
                by_mode_sum[mode] += float(m[mode]["pnl"])
        win_counts[r.get("winner_mode") or "raw"] += 1

    best_global = sorted(
        ok,
        key=lambda r: float((r.get("modes") or {}).get("global", {}).get("pnl") or -1e18),
        reverse=True,
    )
    best_pair = sorted(
        ok,
        key=lambda r: float((r.get("modes") or {}).get("per_pair", {}).get("pnl") or -1e18),
        reverse=True,
    )

    # Best TP/SL per scenario under each mode
    best_sc: dict[str, dict[str, Any]] = {}
    for mode in ("raw", "global", "per_pair"):
        best_sc[mode] = {}
        for r in ok:
            sid = r["scenario_id"]
            pnl = (r.get("modes") or {}).get(mode, {}).get("pnl")
            if pnl is None:
                continue
            cur = best_sc[mode].get(sid)
            if cur is None or float(pnl) > float(cur["pnl"]):
                best_sc[mode][sid] = {
                    "scenario_id": sid,
                    "tp_pct": r["tp_pct"],
                    "sl_pct": r["sl_pct"],
                    "key": r["key"],
                    "pnl": float(pnl),
                    "n": (r.get("modes") or {}).get(mode, {}).get("n"),
                    "winrate": (r.get("modes") or {}).get(mode, {}).get("winrate"),
                }

    return {
        "updated": datetime.now(tz=UTC).isoformat(),
        "exp_id": exp_id,
        "n_cells": len(rows),
        "n_ok": len(ok),
        "sum_test_pnl_by_mode": {k: round(v, 2) for k, v in by_mode_sum.items()},
        "winner_mode_counts": dict(win_counts),
        "best_per_scenario": best_sc,
        "top_global_cells": [
            {
                "key": r["key"],
                "modes": r.get("modes"),
                "winner_mode": r.get("winner_mode"),
                "delta_per_pair_minus_global": r.get("delta_per_pair_minus_global"),
            }
            for r in best_global[:20]
        ],
        "top_per_pair_cells": [
            {
                "key": r["key"],
                "modes": r.get("modes"),
                "winner_mode": r.get("winner_mode"),
                "delta_per_pair_minus_global": r.get("delta_per_pair_minus_global"),
            }
            for r in best_pair[:20]
        ],
        "cells": [
            {
                "key": r.get("key"),
                "scenario_id": r.get("scenario_id"),
                "tp_pct": r.get("tp_pct"),
                "sl_pct": r.get("sl_pct"),
                "modes": r.get("modes"),
                "winner_mode": r.get("winner_mode"),
                "delta_global_minus_raw": r.get("delta_global_minus_raw"),
                "delta_per_pair_minus_raw": r.get("delta_per_pair_minus_raw"),
                "delta_per_pair_minus_global": r.get("delta_per_pair_minus_global"),
                "error": r.get("error"),
            }
            for r in rows
        ],
    }


def write_comparison_md(summary: dict[str, Any], path: Path) -> None:
    lines = [
        "# SL/TP ML gate: raw vs global vs per-pair",
        "",
        f"Updated: {summary.get('updated')}",
        f"Experiment: `{summary.get('exp_id')}`",
        f"Cells: {summary.get('n_ok')}/{summary.get('n_cells')}",
        "",
        "## Sum test PnL by mode",
        "",
    ]
    sums = summary.get("sum_test_pnl_by_mode") or {}
    for mode, pnl in sums.items():
        lines.append(f"- **{mode}**: {pnl:+.2f}")
    lines.extend(["", "## Winner mode counts", ""])
    for mode, n in (summary.get("winner_mode_counts") or {}).items():
        lines.append(f"- {mode}: {n}")
    lines.extend(["", "## Best TP/SL per scenario", ""])
    bps = summary.get("best_per_scenario") or {}
    for mode in ("raw", "global", "per_pair"):
        lines.append(f"### {mode}")
        for sid, row in sorted((bps.get(mode) or {}).items()):
            lines.append(
                f"- `{sid}` TP{row['tp_pct']:g}/SL{row['sl_pct']:g}: "
                f"pnl={row['pnl']:+.2f} n={row.get('n')} WR={row.get('winrate')}"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="SL/TP grid: global vs per-pair ML gates")
    ap.add_argument("--train-range", default=DEFAULT_TRAIN)
    ap.add_argument("--test-range", default=DEFAULT_TEST)
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--exp", default=DEFAULT_EXP)
    ap.add_argument("--scenarios", default="new_psar,trend_macd_ema,trend_supertrend")
    ap.add_argument("--min-pair-train", type=int, default=30)
    ap.add_argument("--min-pair-test", type=int, default=5)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    spec = resolve_exp(args.exp)
    cache_dir = Path(args.cache_dir)
    out_dir = Path(args.out_dir)
    per_dir = out_dir / "cells"
    per_dir.mkdir(parents=True, exist_ok=True)

    want = {s.strip() for s in args.scenarios.split(",") if s.strip()}
    cells = [c for c in discover_cells(cache_dir) if c["scenario_id"] in want]
    if args.limit and args.limit > 0:
        cells = cells[: args.limit]

    te_start, _ = timerange_to_ms(args.test_range)
    print(
        f"=== global vs per-pair ML · cells={len(cells)} · exp={spec['id']} · "
        f"train={args.train_range} test={args.test_range} · "
        f"min_pair_train={args.min_pair_train} ===",
        flush=True,
    )

    store = make_market_store(ROOT)
    rows: list[dict[str, Any]] = []
    t0 = time.time()

    for i, cell in enumerate(cells, 1):
        print(f"\n[{i}/{len(cells)}] {cell['key']}", flush=True)
        try:
            row = run_cell(
                cell=cell,
                spec=spec,
                store=store,
                cut_ms=te_start,
                min_pair_train=args.min_pair_train,
                min_pair_test=args.min_pair_test,
                force=args.force,
                per_dir=per_dir,
            )
        except Exception as exc:
            print(f"  FAILED: {exc}", flush=True)
            row = {
                "scenario_id": cell["scenario_id"],
                "tp_pct": cell["tp_pct"],
                "sl_pct": cell["sl_pct"],
                "key": cell["key"],
                "error": str(exc),
                "traceback": traceback.format_exc()[-1500:],
            }
        rows.append(row)
        modes = row.get("modes") or {}
        print(
            f"  winner={row.get('winner_mode')} "
            f"raw={((modes.get('raw') or {}).get('pnl'))} "
            f"global={((modes.get('global') or {}).get('pnl'))} "
            f"per_pair={((modes.get('per_pair') or {}).get('pnl'))} "
            f"{'(skip)' if row.get('skipped_existing') else ''}",
            flush=True,
        )

        if i % 3 == 0 or i == len(cells):
            summary = build_summary(rows, exp_id=spec["id"])
            (out_dir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    summary = build_summary(rows, exp_id=spec["id"])
    summary["elapsed_sec"] = round(time.time() - t0, 1)
    summary["train_range"] = args.train_range
    summary["test_range"] = args.test_range
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_comparison_md(summary, out_dir / "comparison.md")

    print("\n=== SUM test PnL ===", flush=True)
    for mode, pnl in (summary.get("sum_test_pnl_by_mode") or {}).items():
        print(f"  {mode:10} {pnl:+.2f}", flush=True)
    print("winner_mode_counts:", summary.get("winner_mode_counts"), flush=True)
    print(f"Saved {out_dir / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

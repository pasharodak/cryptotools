#!/usr/bin/env python3
"""Run curated ML-gate experiments on completed SL/TP trade_cache cells.

Cache layout (from collect_sltp_grid_trades.py):
  trade_cache_sltp/{scenario}__sl{XX}__tp{YY}__{train|test}.json

Writes:
  simulation/results/ml_param_experiments/sltp_grid/ml_per_cell/{sid}__slXX__tpYY.json
  simulation/results/ml_param_experiments/sltp_grid/ml_report.json
  simulation/results/ml_param_experiments/sltp_grid/ml_summary.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.pnl_classifier import make_market_store  # noqa: E402
from simulation.scripts.run_ml_param_experiments import (  # noqa: E402
    DEFAULT_TEST,
    DEFAULT_TRAIN,
    all_experiments,
    build_split_frames,
    load_cached,
    run_experiments_on_trades,
    timerange_to_ms,
)
from simulation.scripts.run_scalp_strategies_compare import summarize  # noqa: E402

DEFAULT_CACHE = ROOT / "simulation/results/ml_param_experiments/trade_cache_sltp"
DEFAULT_OUT = ROOT / "simulation/results/ml_param_experiments/sltp_grid"

# Proven winners from prod pack + batch3 — keep pack small for ~200 cells.
CURATED_IDS = [
    "exp14_lgbm_gate045",
    "exp15_lgbm_gate065_nocal",
    "exp08_lgbm_regularized",
    "exp47_voting_soft",
    "exp48_xgb_robust_gate045",
    "exp50_lgbm_gate040_winsor",
    "exp31_mlp",
]

_CELL_RE = re.compile(
    r"^(?P<sid>.+)__sl(?P<sl>\d+(?:p\d+)?)__tp(?P<tp>\d+(?:p\d+)?)__(?P<split>train|test)\.json$"
)


def parse_pct(tag: str) -> float:
    return float(tag.replace("p", "."))


def tag_pct(pct: float) -> str:
    if abs(pct - round(pct)) < 1e-9:
        return f"{int(round(pct)):02d}"
    return f"{pct:.2f}".replace(".", "p")


def discover_cells(cache_dir: Path) -> list[dict[str, Any]]:
    cells: dict[tuple[str, float, float], dict[str, Any]] = {}
    for path in cache_dir.glob("*.json"):
        m = _CELL_RE.match(path.name)
        if not m:
            continue
        sid = m.group("sid")
        sl = parse_pct(m.group("sl"))
        tp = parse_pct(m.group("tp"))
        split = m.group("split")
        key = (sid, tp, sl)
        row = cells.setdefault(
            key,
            {
                "scenario_id": sid,
                "tp_pct": tp,
                "sl_pct": sl,
                "key": f"{sid}__sl{tag_pct(sl)}__tp{tag_pct(tp)}",
                "train_path": None,
                "test_path": None,
            },
        )
        row[f"{split}_path"] = path
    out = [c for c in cells.values() if c["train_path"] and c["test_path"]]
    out.sort(key=lambda c: (c["scenario_id"], c["tp_pct"], c["sl_pct"]))
    return out


def resolve_curated(ids: list[str]) -> list[dict[str, Any]]:
    by_id = {e["id"]: e for e in all_experiments()}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SystemExit(f"Unknown experiment ids: {missing}")
    return [by_id[i] for i in ids]


def cell_report_path(per_dir: Path, cell: dict[str, Any]) -> Path:
    return per_dir / f"{cell['key']}.json"


def run_cell(
    *,
    cell: dict[str, Any],
    experiments: list[dict[str, Any]],
    store: Any,
    cut_ms: int,
    train_range: str,
    test_range: str,
    force: bool,
    per_dir: Path,
    min_train: int = 40,
    min_test: int = 10,
) -> dict[str, Any]:
    path = cell_report_path(per_dir, cell)
    exp_ids = {e["id"] for e in experiments}
    if path.is_file() and not force:
        prev = json.loads(path.read_text(encoding="utf-8"))
        done = {r.get("id") for r in (prev.get("experiments") or []) if not r.get("error")}
        if exp_ids.issubset(done):
            ranked = sorted(
                prev.get("experiments") or [],
                key=lambda r: float(r.get("score") or -1e18),
                reverse=True,
            )
            return {
                "scenario_id": cell["scenario_id"],
                "tp_pct": cell["tp_pct"],
                "sl_pct": cell["sl_pct"],
                "key": cell["key"],
                "winner": prev.get("winner") or (ranked[0].get("id") if ranked else None),
                "score_ml_test_pnl": ranked[0].get("score") if ranked else None,
                "test_raw": prev.get("test_raw"),
                "test_ml": (ranked[0].get("test_ml") if ranked else None),
                "n_train": prev.get("n_train_trades"),
                "n_test": prev.get("n_test_trades"),
                "skipped": True,
                "report_path": str(path),
            }

    train_tr = load_cached(Path(cell["train_path"])) or []
    test_tr = load_cached(Path(cell["test_path"])) or []
    raw = summarize(test_tr)
    base = {
        "scenario_id": cell["scenario_id"],
        "tp_pct": cell["tp_pct"],
        "sl_pct": cell["sl_pct"],
        "key": cell["key"],
        "train_range": train_range,
        "test_range": test_range,
        "n_train_trades": len(train_tr),
        "n_test_trades": len(test_tr),
        "test_raw": raw,
    }
    if len(train_tr) < min_train or len(test_tr) < min_test:
        out = {
            **base,
            "error": f"too few trades train={len(train_tr)} test={len(test_tr)}",
            "experiments": [],
            "winner": None,
            "generated_at": datetime.now(tz=UTC).isoformat(),
        }
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "scenario_id": cell["scenario_id"],
            "tp_pct": cell["tp_pct"],
            "sl_pct": cell["sl_pct"],
            "key": cell["key"],
            "error": out["error"],
            "test_raw": raw,
            "score_ml_test_pnl": None,
        }

    train_df, test_df = build_split_frames(train_tr, test_tr, store, cut_ms)
    results = run_experiments_on_trades(experiments, train_tr, test_tr, train_df, test_df)
    ranked = sorted(results, key=lambda r: float(r.get("score") or -1e18), reverse=True)
    winner = ranked[0] if ranked else None
    out = {
        **base,
        "n_experiments": len(results),
        "experiments": results,
        "ranking": [
            {
                "rank": i + 1,
                "id": r.get("id"),
                "score_ml_test_pnl": r.get("score"),
                "kept": (r.get("test_ml") or {}).get("n"),
                "winrate": (r.get("test_ml") or {}).get("winrate"),
                "auc": (r.get("classifier") or {}).get("roc_auc"),
            }
            for i, r in enumerate(ranked)
        ],
        "winner": (winner or {}).get("id"),
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "scenario_id": cell["scenario_id"],
        "tp_pct": cell["tp_pct"],
        "sl_pct": cell["sl_pct"],
        "key": cell["key"],
        "winner": out["winner"],
        "score_ml_test_pnl": (winner or {}).get("score"),
        "test_raw": raw,
        "test_ml": (winner or {}).get("test_ml"),
        "classifier": (winner or {}).get("classifier"),
        "gate": (winner or {}).get("gate"),
        "n_train": len(train_tr),
        "n_test": len(test_tr),
        "skipped": False,
        "report_path": str(path),
    }


def build_summary(rows: list[dict[str, Any]], *, levels: list[float], exp_ids: list[str]) -> dict[str, Any]:
    ok = [r for r in rows if r.get("score_ml_test_pnl") is not None]
    ranked = sorted(ok, key=lambda r: float(r["score_ml_test_pnl"]), reverse=True)

    # Best cell per scenario
    best_sc: dict[str, dict[str, Any]] = {}
    for r in ranked:
        sid = r["scenario_id"]
        if sid not in best_sc:
            best_sc[sid] = r

    # Aggregate by TP/SL using winner score
    from collections import defaultdict

    by_cell: dict[tuple[float, float], list[dict[str, Any]]] = defaultdict(list)
    for r in ok:
        by_cell[(float(r["tp_pct"]), float(r["sl_pct"]))].append(r)

    agg_cells = []
    for (tp, sl), xs in sorted(by_cell.items(), key=lambda kv: -sum(float(r["score_ml_test_pnl"]) for r in kv[1])):
        pnls = [float(r["score_ml_test_pnl"]) for r in xs]
        raw_pnls = [float((r.get("test_raw") or {}).get("pnl") or 0) for r in xs]
        ml_trades = sum(int((r.get("test_ml") or {}).get("n") or 0) for r in xs)
        ml_wins = sum(int((r.get("test_ml") or {}).get("wins") or 0) for r in xs)
        ml_losses = sum(int((r.get("test_ml") or {}).get("losses") or 0) for r in xs)
        agg_cells.append(
            {
                "tp_pct": tp,
                "sl_pct": sl,
                "scenarios": len(xs),
                "sum_ml_pnl": round(sum(pnls), 2),
                "mean_ml_pnl": round(sum(pnls) / len(pnls), 2),
                "sum_raw_pnl": round(sum(raw_pnls), 2),
                "ml_trades": ml_trades,
                "ml_wins": ml_wins,
                "ml_losses": ml_losses,
                "ml_winrate": round(ml_wins / ml_trades, 4) if ml_trades else None,
            }
        )

    return {
        "updated": datetime.now(tz=UTC).isoformat(),
        "levels_pct": levels,
        "experiment_ids": exp_ids,
        "n_cells": len(rows),
        "n_ok": len(ok),
        "aggregate_by_tp_sl": agg_cells,
        "best_overall_top30": [
            {
                "scenario_id": r["scenario_id"],
                "tp_pct": r["tp_pct"],
                "sl_pct": r["sl_pct"],
                "winner": r.get("winner"),
                "score_ml_test_pnl": r.get("score_ml_test_pnl"),
                "test_ml": r.get("test_ml"),
                "test_raw": r.get("test_raw"),
            }
            for r in ranked[:30]
        ],
        "best_per_scenario": [
            {
                "scenario_id": sid,
                "tp_pct": best_sc[sid]["tp_pct"],
                "sl_pct": best_sc[sid]["sl_pct"],
                "winner": best_sc[sid].get("winner"),
                "score_ml_test_pnl": best_sc[sid].get("score_ml_test_pnl"),
                "test_ml": best_sc[sid].get("test_ml"),
                "test_raw": best_sc[sid].get("test_raw"),
            }
            for sid in sorted(best_sc)
        ],
        "positive_ml_cells": sum(1 for r in ok if float(r["score_ml_test_pnl"]) > 0),
        "sum_best_per_scenario_ml_pnl": round(
            sum(float(best_sc[s]["score_ml_test_pnl"]) for s in best_sc), 2
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="ML gate on SL/TP grid trade cache")
    ap.add_argument("--train-range", default=DEFAULT_TRAIN)
    ap.add_argument("--test-range", default=DEFAULT_TEST)
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument(
        "--exps",
        default=",".join(CURATED_IDS),
        help="comma experiment ids (default curated winners)",
    )
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="max cells to newly train this run")
    ap.add_argument("--scenarios", default="", help="optional comma filter")
    args = ap.parse_args()

    cache_dir = Path(args.cache_dir)
    out_dir = Path(args.out_dir)
    per_dir = out_dir / "ml_per_cell"
    per_dir.mkdir(parents=True, exist_ok=True)

    exp_ids = [x.strip() for x in args.exps.split(",") if x.strip()]
    experiments = resolve_curated(exp_ids)
    cells = discover_cells(cache_dir)
    if args.scenarios.strip():
        want = {s.strip() for s in args.scenarios.split(",") if s.strip()}
        cells = [c for c in cells if c["scenario_id"] in want]

    te_start, _ = timerange_to_ms(args.test_range)
    print(
        f"=== ML on SL/TP grid · cells={len(cells)} · exps={len(experiments)} · "
        f"train={args.train_range} test={args.test_range} ===",
        flush=True,
    )
    for e in experiments:
        print(f"  - {e['id']}", flush=True)

    store = make_market_store(ROOT)
    rows: list[dict[str, Any]] = []
    trained = 0
    t0 = time.time()

    for i, cell in enumerate(cells, 1):
        path = cell_report_path(per_dir, cell)
        already = path.is_file() and not args.force
        if already:
            # Still load summary row; run_cell will skip work
            pass
        elif args.limit and trained >= args.limit:
            # Keep prior report if any
            if path.is_file():
                prev = json.loads(path.read_text(encoding="utf-8"))
                ranked = sorted(
                    prev.get("experiments") or [],
                    key=lambda r: float(r.get("score") or -1e18),
                    reverse=True,
                )
                rows.append(
                    {
                        "scenario_id": cell["scenario_id"],
                        "tp_pct": cell["tp_pct"],
                        "sl_pct": cell["sl_pct"],
                        "key": cell["key"],
                        "winner": prev.get("winner"),
                        "score_ml_test_pnl": ranked[0].get("score") if ranked else None,
                        "test_raw": prev.get("test_raw"),
                        "test_ml": ranked[0].get("test_ml") if ranked else None,
                        "skipped_limit": True,
                    }
                )
            continue

        print(
            f"\n[{i}/{len(cells)}] {cell['key']}",
            flush=True,
        )
        try:
            row = run_cell(
                cell=cell,
                experiments=experiments,
                store=store,
                cut_ms=te_start,
                train_range=args.train_range,
                test_range=args.test_range,
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
        if not row.get("skipped") and not row.get("error"):
            trained += 1
        ml = row.get("test_ml") or {}
        raw = row.get("test_raw") or {}
        print(
            f"  winner={row.get('winner')} ml_pnl={row.get('score_ml_test_pnl')} "
            f"kept={ml.get('n')} WR={ml.get('winrate')} | raw_pnl={raw.get('pnl')} "
            f"{'(skip)' if row.get('skipped') else ''}",
            flush=True,
        )

        # periodic report flush
        if i % 5 == 0 or i == len(cells):
            report = {
                "updated": datetime.now(tz=UTC).isoformat(),
                "train_range": args.train_range,
                "test_range": args.test_range,
                "experiment_ids": exp_ids,
                "n_cells": len(cells),
                "n_rows": len(rows),
                "elapsed_sec": round(time.time() - t0, 1),
                "rows": rows,
            }
            (out_dir / "ml_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    levels = sorted({float(c["tp_pct"]) for c in cells} | {float(c["sl_pct"]) for c in cells})
    summary = build_summary(rows, levels=levels, exp_ids=exp_ids)
    (out_dir / "ml_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n=== Aggregate ML test PnL by TP/SL ===", flush=True)
    for c in summary["aggregate_by_tp_sl"]:
        print(
            f"  TP{c['tp_pct']:g}/SL{c['sl_pct']:g}: scen={c['scenarios']} "
            f"sum_ml={c['sum_ml_pnl']:+.1f} mean={c['mean_ml_pnl']:+.1f} "
            f"raw_sum={c['sum_raw_pnl']:+.1f} "
            f"kept={c['ml_trades']} WR={(c['ml_winrate'] or 0)*100:.1f}%",
            flush=True,
        )
    print(
        f"\npositive_ml_cells={summary['positive_ml_cells']}/{summary['n_ok']} "
        f"sum_best_per_scenario={summary['sum_best_per_scenario_ml_pnl']}",
        flush=True,
    )
    print("Top 10 ML cells:", flush=True)
    for r in summary["best_overall_top30"][:10]:
        ml = r.get("test_ml") or {}
        print(
            f"  {r['scenario_id']:22} TP{r['tp_pct']:g}/SL{r['sl_pct']:g} "
            f"{r.get('winner')} ml_pnl={r.get('score_ml_test_pnl')} "
            f"WR={ml.get('winrate')} n={ml.get('n')}",
            flush=True,
        )
    print(f"Saved {out_dir / 'ml_summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Summarize completed SL/TP grid cells from cache/manifest (partial OK)."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "ml_param_experiments" / "sltp_grid"
CACHE = ROOT / "results" / "ml_param_experiments" / "trade_cache_sltp"
PACK = ROOT / "config" / "prod_top30_pack.json"


def main() -> None:
    top30 = [
        s["scenario_id"]
        for s in json.loads(PACK.read_text(encoding="utf-8")).get("strategies") or []
        if s.get("scenario_id")
    ]
    levels = [3.0, 8.0, 13.0]
    expected = {(sid, tp, sl) for sid in top30 for tp in levels for sl in levels}

    manifest = {}
    mp = OUT / "manifest.json"
    if mp.is_file():
        manifest = json.loads(mp.read_text(encoding="utf-8"))

    by_key = {}
    for r in manifest.get("results") or []:
        if r.get("error") or not r.get("test"):
            continue
        key = (r["scenario_id"], float(r["tp_pct"]), float(r["sl_pct"]))
        by_key[key] = r

    # Fill from cache filenames if manifest incomplete
    for tr in CACHE.glob("*__train.json"):
        name = tr.name.replace("__train.json", "")
        te = CACHE / f"{name}__test.json"
        if not te.is_file():
            continue
        # name: {sid}__slXX__tpYY
        try:
            sid, sl_tag, tp_tag = name.rsplit("__", 2)
            sl = float(sl_tag.replace("sl", "").replace("p", "."))
            tp = float(tp_tag.replace("tp", "").replace("p", "."))
        except Exception:
            continue
        key = (sid, tp, sl)
        if key in by_key:
            continue
        test = json.loads(te.read_text(encoding="utf-8"))
        train = json.loads(tr.read_text(encoding="utf-8"))
        n = len(test)
        wins = sum(1 for t in test if float(t.get("profit_abs") or 0) >= 0)
        pnl = sum(float(t.get("profit_abs") or 0) for t in test)
        by_key[key] = {
            "scenario_id": sid,
            "tp_pct": tp,
            "sl_pct": sl,
            "n_train": len(train),
            "n_test": n,
            "test": {
                "n": n,
                "wins": wins,
                "losses": n - wins,
                "winrate": round(wins / n, 4) if n else 0.0,
                "pnl": round(pnl, 4),
                "avg_pnl": round(pnl / n, 4) if n else 0.0,
            },
            "train": {
                "n": len(train),
                "pnl": round(sum(float(t.get("profit_abs") or 0) for t in train), 4),
            },
        }

    done = set(by_key)
    missing = sorted(expected - done)
    rows = list(by_key.values())

    # Best overall by test pnl
    ranked = sorted(rows, key=lambda r: float((r.get("test") or {}).get("pnl") or -1e18), reverse=True)

    # Best SL/TP per scenario
    best_per_sc = {}
    for r in ranked:
        sid = r["scenario_id"]
        if sid not in best_per_sc:
            best_per_sc[sid] = r

    # Aggregate by (tp,sl)
    by_cell = defaultdict(lambda: {"n_scen": 0, "pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
    for r in rows:
        k = (float(r["tp_pct"]), float(r["sl_pct"]))
        t = r.get("test") or {}
        by_cell[k]["n_scen"] += 1
        by_cell[k]["pnl"] += float(t.get("pnl") or 0)
        by_cell[k]["trades"] += int(t.get("n") or 0)
        by_cell[k]["wins"] += int(t.get("wins") or 0)
        by_cell[k]["losses"] += int(t.get("losses") or 0)

    cell_rows = []
    for (tp, sl), a in sorted(by_cell.items(), key=lambda x: x[1]["pnl"], reverse=True):
        n = a["trades"]
        cell_rows.append(
            {
                "tp_pct": tp,
                "sl_pct": sl,
                "scenarios": a["n_scen"],
                "trades": n,
                "wins": a["wins"],
                "losses": a["losses"],
                "winrate": round(a["wins"] / n, 4) if n else 0.0,
                "pnl": round(a["pnl"], 2),
            }
        )

    # Coverage matrix scenario x cell
    coverage = {sid: [] for sid in top30}
    for sid, tp, sl in sorted(done):
        if sid in coverage:
            coverage[sid].append(f"TP{tp:g}/SL{sl:g}")

    summary = {
        "status": "partial_stopped",
        "levels_pct": levels,
        "expected_jobs": len(expected),
        "completed_jobs": len(done & expected),
        "missing_jobs": len(missing),
        "missing_sample": [
            {"scenario_id": s, "tp_pct": tp, "sl_pct": sl} for s, tp, sl in missing[:30]
        ],
        "aggregate_by_tp_sl": cell_rows,
        "best_overall_top20": [
            {
                "scenario_id": r["scenario_id"],
                "tp_pct": r["tp_pct"],
                "sl_pct": r["sl_pct"],
                "test": r.get("test"),
                "n_train": r.get("n_train"),
            }
            for r in ranked[:20]
        ],
        "best_per_scenario": [
            {
                "scenario_id": sid,
                "tp_pct": best_per_sc[sid]["tp_pct"],
                "sl_pct": best_per_sc[sid]["sl_pct"],
                "test": best_per_sc[sid].get("test"),
            }
            for sid in top30
            if sid in best_per_sc
        ],
        "coverage_counts": {sid: len(coverage.get(sid) or []) for sid in top30},
    }
    out_path = OUT / "partial_summary.json"
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"completed {summary['completed_jobs']}/{summary['expected_jobs']}  missing {summary['missing_jobs']}")
    print("\n=== Aggregate test PnL by TP/SL (across available scenarios) ===")
    for c in cell_rows:
        print(
            f"  TP{c['tp_pct']:g}/SL{c['sl_pct']:g}: scen={c['scenarios']:2d} "
            f"trades={c['trades']:6d} WR={c['winrate']*100:5.1f}% pnl={c['pnl']:+8.1f}"
        )
    print("\n=== Top 10 cells (scenario + TP/SL) by test PnL ===")
    for r in ranked[:10]:
        t = r["test"]
        print(
            f"  {r['scenario_id']:22} TP{r['tp_pct']:g}/SL{r['sl_pct']:g} "
            f"n={t['n']:5d} WR={t['winrate']*100:5.1f}% pnl={t['pnl']:+7.1f}"
        )
    print("\n=== Best TP/SL per scenario (have data) ===")
    for row in summary["best_per_scenario"]:
        t = row["test"]
        print(
            f"  {row['scenario_id']:22} TP{row['tp_pct']:g}/SL{row['sl_pct']:g} "
            f"WR={t['winrate']*100:5.1f}% pnl={t['pnl']:+7.1f} n={t['n']}"
        )
    miss_by = defaultdict(int)
    for s, tp, sl in missing:
        miss_by[s] += 1
    print("\n=== Missing cells per scenario ===")
    for sid in top30:
        m = miss_by.get(sid, 0)
        if m:
            print(f"  {sid:22} missing {m}/9  have {9-m}")
    print("Saved", out_path)


if __name__ == "__main__":
    main()

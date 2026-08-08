#!/usr/bin/env python3
"""Summarize batch3 vs previous winners for top-30 scenarios."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PER = ROOT / "simulation/results/ml_param_experiments/per_scenario"
PACK = ROOT / "simulation/config/prod_top30_pack.json"
OUT = ROOT / "simulation/results/ml_param_experiments/batch3_vs_top30.json"


def exp_num(eid):
    if not eid or not str(eid).startswith("exp"):
        return None
    digits = ""
    for ch in str(eid)[3:]:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


def main() -> int:
    pack = json.loads(PACK.read_text(encoding="utf-8"))
    rows = []
    batch3_wins = 0
    improved = 0
    for s in pack.get("strategies") or []:
        sid = s["scenario_id"]
        path = PER / f"{sid}.json"
        if not path.is_file():
            rows.append({"scenario_id": sid, "status": "missing"})
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        exps = [r for r in (data.get("experiments") or []) if not r.get("error")]
        b3 = [r for r in exps if (n := exp_num(r.get("id"))) and 31 <= n <= 50]
        old = [r for r in exps if (n := exp_num(r.get("id"))) and n <= 30]
        best_old = max(old, key=lambda r: float(r.get("score") or -1e18), default=None)
        best_new = max(b3, key=lambda r: float(r.get("score") or -1e18), default=None)
        overall = max(exps, key=lambda r: float(r.get("score") or -1e18), default=None)
        old_score = float(best_old["score"]) if best_old else None
        new_score = float(best_new["score"]) if best_new else None
        if best_new and overall and overall.get("id") == best_new.get("id"):
            batch3_wins += 1
        if old_score is not None and new_score is not None and new_score > old_score + 0.01:
            improved += 1
        delta = None if old_score is None or new_score is None else round(new_score - old_score, 4)
        rows.append(
            {
                "rank_prod": s.get("rank"),
                "scenario_id": sid,
                "label": s.get("label"),
                "prod_winner": s.get("winner_exp"),
                "prod_score": s.get("score_ml_test_pnl"),
                "best_old": best_old["id"] if best_old else None,
                "best_old_score": old_score,
                "best_batch3": best_new["id"] if best_new else None,
                "best_batch3_score": new_score,
                "overall_winner": overall["id"] if overall else None,
                "overall_score": float(overall["score"]) if overall else None,
                "n_batch3_done": len(b3),
                "delta_vs_old": delta,
            }
        )

    rows_sorted = sorted(
        [r for r in rows if r.get("best_batch3_score") is not None],
        key=lambda r: float(r["best_batch3_score"]),
        reverse=True,
    )
    report = {
        "n_scenarios": len(rows),
        "n_with_batch3": sum(1 for r in rows if (r.get("n_batch3_done") or 0) > 0),
        "batch3_is_overall_winner": batch3_wins,
        "batch3_beats_old": improved,
        "by_scenario": rows,
        "batch3_ranking": [
            {
                "rank": i + 1,
                "scenario_id": r["scenario_id"],
                "best_batch3": r["best_batch3"],
                "score": r["best_batch3_score"],
                "delta_vs_old": r["delta_vs_old"],
                "overall_winner": r["overall_winner"],
            }
            for i, r in enumerate(rows_sorted)
        ],
    }
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"scenarios={report['n_scenarios']} with_b3={report['n_with_batch3']}")
    print(f"batch3 overall winners={batch3_wins}  beats_old={improved}")
    print("Top improvements (batch3 vs old):")
    improved_rows = sorted(
        [r for r in rows if r.get("delta_vs_old") is not None],
        key=lambda r: r["delta_vs_old"],
        reverse=True,
    )
    for r in improved_rows[:15]:
        print(
            f"  {r['scenario_id']}: {r['best_old']} {r['best_old_score']:.2f} -> "
            f"{r['best_batch3']} {r['best_batch3_score']:.2f} (delta={r['delta_vs_old']:+.2f})"
        )
    print("Overall winner frequency:")
    from collections import Counter
    print(Counter(r.get("overall_winner") for r in rows))
    print("Batch3 best frequency:")
    print(Counter(r.get("best_batch3") for r in rows))
    print(f"Saved {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Per-pair / per-strategy stats for Apr-Jun 2026 raw vs ML gate."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from simulation.ml.pnl_classifier import load_trades
from simulation.ml.trade_gate import MlEntryGate
from simulation.exchange_sim.bot_session import load_bot_scenarios

OUT = ROOT / "simulation/results/all_bots_period_ml/pair_strategy_stats.json"
START_MS = 1775001600000  # 2026-04-01
END_MS = 1782863999000    # 2026-06-30
SCENARIOS = {
    s["id"]: s
    for s in load_bot_scenarios(ROOT)
    if s["id"]
    not in {"live_strategy", "live_freqai", "live_grid", "live_grid_safe"}
}


def empty() -> dict[str, Any]:
    return {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0}


def add(bucket: dict, pnl: float) -> None:
    bucket["trades"] += 1
    bucket["pnl"] += pnl
    if pnl >= 0:
        bucket["wins"] += 1
    else:
        bucket["losses"] += 1


def finalize(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        t = r["trades"]
        out.append(
            {
                **r,
                "pnl": round(r["pnl"], 4),
                "win_rate": round(r["wins"] / t, 4) if t else 0.0,
            }
        )
    out.sort(key=lambda x: x["pnl"], reverse=True)
    return out


def main() -> int:
    db = ROOT / "simulation/results/trade_db"
    all_recs = load_trades(db)
    # Filter to period + known scenarios
    recs = []
    for rec in all_recs:
        sid = rec.get("scenario_id") or (rec.get("basis") or {}).get("scenario_id")
        if sid not in SCENARIOS:
            continue
        tr = rec.get("trade") or {}
        open_ms = int(tr.get("open_ms") or 0)
        if open_ms < START_MS or open_ms >= END_MS:
            continue
        recs.append(rec)
    print(f"loaded {len(recs)} period trades", flush=True)

    gate = MlEntryGate(ROOT)
    gate.set_enabled(True)
    print("gate", gate.status(), flush=True)

    # Group by scenario,pair for ML evaluate
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for rec in recs:
        sid = rec.get("scenario_id") or (rec.get("basis") or {}).get("scenario_id")
        pair = rec.get("pair") or (rec.get("basis") or {}).get("pair")
        groups[(sid, pair)].append(rec)

    raw_by_pair: dict[str, dict] = defaultdict(empty)
    raw_by_strat: dict[str, dict] = defaultdict(empty)
    ml_by_pair: dict[str, dict] = defaultdict(empty)
    ml_by_strat: dict[str, dict] = defaultdict(empty)
    ml_by_pair_strat: dict[tuple[str, str], dict] = defaultdict(empty)

    kept_n = skipped_n = 0
    skipped_pnl = 0.0

    for i, ((sid, pair), items) in enumerate(groups.items(), 1):
        sc = SCENARIOS[sid]
        trades = []
        for rec in items:
            tr = dict(rec.get("trade") or {})
            tr["pair"] = pair
            tr["profit_abs"] = float(rec.get("profit_abs") or tr.get("profit_abs") or 0)
            trades.append(tr)
        # sort by open
        trades.sort(key=lambda t: t.get("open_ms") or 0)
        cfg = {
            "stake": (sc.get("stake_usdt")),
            "stoploss": None,
            "minimal_roi": None,
            "timeframe": "5m",
        }
        # pull config-ish from first record basis
        basis0 = (items[0].get("basis") or {})
        cfg["stake"] = basis0.get("stake_usdt") or cfg["stake"]
        cfg["stoploss"] = basis0.get("stoploss")
        cfg["minimal_roi"] = basis0.get("minimal_roi")
        cfg["timeframe"] = basis0.get("timeframe") or "5m"

        for tr in trades:
            pnl = float(tr.get("profit_abs") or 0)
            add(raw_by_pair[pair], pnl)
            add(raw_by_strat[sid], pnl)

        kept, skipped, _stats = gate.evaluate_trades(
            trades, sc, pair, cfg, armed_at_ms=START_MS
        )
        for tr in kept:
            pnl = float(tr.get("profit_abs") or 0)
            add(ml_by_pair[pair], pnl)
            add(ml_by_strat[sid], pnl)
            add(ml_by_pair_strat[(pair, sid)], pnl)
            kept_n += 1
        for tr in skipped:
            skipped_n += 1
            skipped_pnl += float(tr.get("profit_abs") or 0)

        if i % 200 == 0:
            print(f"  gated {i}/{len(groups)} groups...", flush=True)

    def pack(d: dict[str, dict], key_name: str) -> list[dict]:
        rows = [{key_name: k, **v} for k, v in d.items()]
        return finalize(rows)

    pair_strat_rows = finalize(
        [
            {"pair": p, "scenario_id": s, **v}
            for (p, s), v in ml_by_pair_strat.items()
        ]
    )

    ml_pairs = pack(ml_by_pair, "pair")
    raw_pairs = pack(raw_by_pair, "pair")
    ml_strats = pack(ml_by_strat, "scenario_id")
    raw_strats = pack(raw_by_strat, "scenario_id")

    profitable_pairs = [p for p in ml_pairs if p["pnl"] > 0]
    losing_pairs = [p for p in ml_pairs if p["pnl"] < 0]
    flat_pairs = [p for p in ml_pairs if p["pnl"] == 0]

    out = {
        "period": "20260401-20260625",
        "n_raw_trades": len(recs),
        "n_ml_kept": kept_n,
        "n_ml_skipped": skipped_n,
        "ml_skipped_pnl": round(skipped_pnl, 4),
        "ml_total_pnl": round(sum(p["pnl"] for p in ml_pairs), 4),
        "raw_total_pnl": round(sum(p["pnl"] for p in raw_pairs), 4),
        "n_pairs_ml": len(ml_pairs),
        "n_pairs_profitable": len(profitable_pairs),
        "n_pairs_losing": len(losing_pairs),
        "strategies_ml": ml_strats,
        "strategies_raw": raw_strats,
        "top_pairs_ml": ml_pairs[:25],
        "bottom_pairs_ml": list(reversed(ml_pairs[-25:])),
        "top_pair_strategy_ml": pair_strat_rows[:30],
        "bottom_pair_strategy_ml": list(reversed(pair_strat_rows[-30:])),
        "all_pairs_ml": ml_pairs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    # CSV
    import csv

    csv_path = OUT.with_name("pairs_ml.csv")
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["pair", "trades", "pnl", "wins", "losses", "win_rate"])
        w.writeheader()
        for r in ml_pairs:
            w.writerow(r)

    print("\n=== STRATEGIES ML ===")
    for s in ml_strats:
        print(f"  {s['scenario_id']:18s} pnl={s['pnl']:+8.2f} n={s['trades']:5d} WR={s['win_rate']*100:5.1f}%")
    print(f"\nProfitable pairs: {len(profitable_pairs)} / {len(ml_pairs)}")
    print("TOP 15 pairs ML:")
    for p in ml_pairs[:15]:
        print(f"  {p['pair']:22s} pnl={p['pnl']:+8.2f} n={p['trades']:4d} WR={p['win_rate']*100:5.1f}%")
    print("BOTTOM 15 pairs ML:")
    for p in list(reversed(ml_pairs[-15:])):
        print(f"  {p['pair']:22s} pnl={p['pnl']:+8.2f} n={p['trades']:4d} WR={p['win_rate']*100:5.1f}%")
    print(f"\nWrote {OUT}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

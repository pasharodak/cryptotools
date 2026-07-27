#!/usr/bin/env python3
"""Run ALL real bots on prod200; with --ml also aggregates ML-gated trades vs raw."""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import BotSessionManager
from simulation.exchange_sim.player_sync import summarize_trades
from simulation.exchange_sim.trade_archive import persist_session
from simulation.scripts.comparison_common import PROD_PAIRS_PATH, pairs_from_source, period_months
from simulation.scripts.compare_ml_by_month import month_range

SKIP_STUBS = {"live_strategy", "live_freqai", "live_grid", "live_grid_safe"}


def enable_all_real(mgr: BotSessionManager) -> list[str]:
    ids: list[str] = []
    for sc in mgr.scenarios:
        sid = sc["id"]
        if sid in SKIP_STUBS:
            sc["enabled"] = False
            continue
        sc["enabled"] = True
        ids.append(sid)
    return ids


def _bucket_add(by_bot: dict, inst: dict, s: dict) -> None:
    sid = inst.get("scenario_id") or "unknown"
    n = int(s.get("total_trades") or 0)
    pnl = float(s.get("profit_abs") or 0)
    w = int(s.get("wins") or 0)
    l = int(s.get("losses") or 0)
    bucket = by_bot.setdefault(
        sid,
        {
            "scenario_id": sid,
            "label": inst.get("label"),
            "strategy": inst.get("strategy"),
            "group": inst.get("group"),
            "trades": 0,
            "pnl_usdt": 0.0,
            "wins": 0,
            "losses": 0,
            "pairs_with_trades": set(),
            "ml_skipped": 0,
            "ml_skipped_pnl": 0.0,
        },
    )
    bucket["trades"] += n
    bucket["pnl_usdt"] += pnl
    bucket["wins"] += w
    bucket["losses"] += l
    if n > 0 and inst.get("pair"):
        bucket["pairs_with_trades"].add(inst["pair"])
    skipped = inst.get("ml_skipped_trades") or []
    bucket["ml_skipped"] += len(skipped)
    bucket["ml_skipped_pnl"] += sum(float(t.get("profit_abs") or 0) for t in skipped)


def _finalize_bots(by_bot: dict) -> tuple[list[dict], dict]:
    bots_out = []
    total_trades = total_pnl = wins = losses = skipped = 0
    skipped_pnl = 0.0
    for b in by_bot.values():
        t = b["trades"]
        total_trades += t
        total_pnl += b["pnl_usdt"]
        wins += b["wins"]
        losses += b["losses"]
        skipped += b["ml_skipped"]
        skipped_pnl += b["ml_skipped_pnl"]
        bots_out.append(
            {
                "scenario_id": b["scenario_id"],
                "label": b["label"],
                "strategy": b["strategy"],
                "group": b["group"],
                "trades": t,
                "pnl_usdt": round(b["pnl_usdt"], 4),
                "wins": b["wins"],
                "losses": b["losses"],
                "win_rate": round(b["wins"] / t, 4) if t else 0.0,
                "pairs_with_trades": len(b["pairs_with_trades"]),
                "ml_skipped": b["ml_skipped"],
                "ml_skipped_pnl": round(b["ml_skipped_pnl"], 4),
            }
        )
    bots_out.sort(key=lambda x: x["pnl_usdt"], reverse=True)
    return bots_out, {
        "trades": total_trades,
        "pnl_usdt": round(total_pnl, 4),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total_trades, 4) if total_trades else 0.0,
        "ml_skipped": skipped,
        "ml_skipped_pnl": round(skipped_pnl, 4),
        "bots": bots_out,
    }


def agg_instances(instances: list[dict], *, use_ml: bool) -> dict[str, Any]:
    by_raw: dict = {}
    by_ml: dict = {}
    for inst in instances:
        raw_s = inst.get("raw_summary") or summarize_trades(inst.get("raw_trades") or [])
        ml_s = inst.get("ml_summary") or summarize_trades(inst.get("trades") or [])
        _bucket_add(by_raw, inst, raw_s)
        _bucket_add(by_ml, inst, ml_s)
    _, raw = _finalize_bots(by_raw)
    _, ml = _finalize_bots(by_ml)
    primary = ml if use_ml else raw
    return {"primary": primary, "raw": raw, "ml": ml}


def run_month(pairs, year, month, workers, *, use_ml: bool):
    start_ms, end_ms, label = month_range(year, month)
    datadir = ROOT / "simulation/data/freqtrade"
    t0 = time.time()
    mgr = BotSessionManager(ROOT)
    order = enable_all_real(mgr)
    mgr._get_ml_gate().set_enabled(bool(use_ml))
    gate_st = mgr._get_ml_gate().status()
    print(
        f"  [{label}] init · {len(pairs)} pairs · {len(order)} bots · "
        f"ML={'ON' if use_ml else 'OFF'} ready={gate_st.get('ready')}",
        flush=True,
    )
    mgr.instances = []
    mgr.scan_events = []
    mgr.status = {
        "running": True,
        "phase": "simulating",
        "scenarios": {},
        "instances": [],
        "selected_id": None,
        "pairs": pairs,
        "range_ms": [start_ms, end_ms],
        "pool_mode": "direct",
        "pool": list(pairs),
        "sim_pool": list(pairs),
    }
    mgr.scan_schedule = {
        "rolling_whitelist": False,
        "grid_arms": {p: start_ms for p in pairs},
        "strategy_arms": {p: start_ms for p in pairs},
        "grid_timeline": [],
        "strategy_timeline": [],
        "whitelist_grace_scans": 0,
        "trade_skip_loss_streak": 0,
        "events": [],
        "scan_interval_ms": 900000,
    }
    pool = list(pairs)

    def load_one(sid: str) -> str:
        print(f"  [{label}] backtest {sid}...", flush=True)
        mgr.load_scenario_instances(sid, pool, start_ms, end_ms, datadir)
        return sid

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(load_one, sid): sid for sid in order}
        for fut in as_completed(futs):
            sid = futs[fut]
            try:
                fut.result()
                print(f"  [{label}] done {sid}", flush=True)
            except Exception as exc:
                print(f"  [{label}] FAIL {sid}: {exc}", flush=True)

    summaries = agg_instances(list(mgr.instances), use_ml=use_ml)
    archive = persist_session(mgr, ROOT, source=f"all_bots_period_ml_{label}" if use_ml else f"all_bots_period_{label}")
    elapsed = round(time.time() - t0, 1)
    return {
        "month": label,
        "pairs": len(pool),
        "bots": len(order),
        "bot_ids": order,
        "elapsed_sec": elapsed,
        "summary": summaries["primary"],
        "summary_raw": summaries["raw"],
        "summary_ml": summaries["ml"],
        "archive": {
            "run_id": archive.get("run_id"),
            "trades_saved": archive.get("trades_saved"),
            "net_usdt": archive.get("net_usdt"),
        },
    }


def merge_months(months, key: str = "summary"):
    by_bot = {}
    total_trades = total_pnl = wins = losses = skipped = 0
    skipped_pnl = 0.0
    for m in months:
        s = m[key]
        total_trades += s["trades"]
        total_pnl += s["pnl_usdt"]
        wins += s["wins"]
        losses += s["losses"]
        skipped += int(s.get("ml_skipped") or 0)
        skipped_pnl += float(s.get("ml_skipped_pnl") or 0)
        for b in s["bots"]:
            sid = b["scenario_id"]
            bucket = by_bot.setdefault(
                sid,
                {
                    "scenario_id": sid,
                    "label": b["label"],
                    "strategy": b["strategy"],
                    "group": b["group"],
                    "trades": 0,
                    "pnl_usdt": 0.0,
                    "wins": 0,
                    "losses": 0,
                    "pairs_with_trades": 0,
                    "ml_skipped": 0,
                    "ml_skipped_pnl": 0.0,
                    "months": {},
                },
            )
            bucket["trades"] += b["trades"]
            bucket["pnl_usdt"] += b["pnl_usdt"]
            bucket["wins"] += b["wins"]
            bucket["losses"] += b["losses"]
            bucket["pairs_with_trades"] = max(bucket["pairs_with_trades"], b["pairs_with_trades"])
            bucket["ml_skipped"] += int(b.get("ml_skipped") or 0)
            bucket["ml_skipped_pnl"] += float(b.get("ml_skipped_pnl") or 0)
            bucket["months"][m["month"]] = {
                "trades": b["trades"],
                "pnl_usdt": b["pnl_usdt"],
                "win_rate": b["win_rate"],
            }

    bots_out = []
    for b in by_bot.values():
        t = b["trades"]
        bots_out.append(
            {
                **{k: v for k, v in b.items() if k != "pnl_usdt"},
                "pnl_usdt": round(b["pnl_usdt"], 4),
                "ml_skipped_pnl": round(b["ml_skipped_pnl"], 4),
                "win_rate": round(b["wins"] / t, 4) if t else 0.0,
            }
        )
    bots_out.sort(key=lambda x: x["pnl_usdt"], reverse=True)
    return {
        "trades": total_trades,
        "pnl_usdt": round(total_pnl, 4),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total_trades, 4) if total_trades else 0.0,
        "ml_skipped": skipped,
        "ml_skipped_pnl": round(skipped_pnl, 4),
        "bots": bots_out,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timerange", default="20260401-20260625")
    ap.add_argument("--pairs", default="prod200")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--limit-pairs", type=int, default=0)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--ml", action="store_true", help="Enable ML entry gate")
    ap.add_argument(
        "--out-dir",
        default="",
        help="Output dir under simulation/results (default: all_bots_period or all_bots_period_ml)",
    )
    args = ap.parse_args()

    use_ml = bool(args.ml)
    out_name = args.out_dir or ("all_bots_period_ml" if use_ml else "all_bots_period")
    out_dir = ROOT / "simulation/results" / out_name

    start_s, end_s = args.timerange.split("-")
    y0, m0 = int(start_s[:4]), int(start_s[4:6])
    y1, m1 = int(end_s[:4]), int(end_s[4:6])
    months = period_months(y0, m0, y1, m1)

    if args.pairs in ("prod200", "prod"):
        pairs = list(json.loads((ROOT / PROD_PAIRS_PATH).read_text(encoding="utf-8")).get("pairs") or [])
    else:
        pairs = pairs_from_source(ROOT, args.pairs)
    if args.limit_pairs:
        pairs = pairs[: args.limit_pairs]
    _drop = {"ZRO/USDT:USDT", "ZK/USDT:USDT"}
    pairs = [p for p in pairs if p not in _drop]

    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "state.json"
    if args.fresh and state_path.is_file():
        state_path.unlink()
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {"months": []}
    done = {m["month"] for m in state.get("months") or []}

    print(
        f"=== ALL BOTS PERIOD · {len(pairs)} pairs · {args.timerange} · "
        f"{len(months)} months · workers={args.workers} · ML={'ON' if use_ml else 'OFF'} ===",
        flush=True,
    )

    month_rows = list(state.get("months") or [])
    for y, m in months:
        label = f"{y}-{m:02d}"
        if label in done:
            print(f"  skip {label} (already done)", flush=True)
            continue
        row = run_month(pairs, y, m, args.workers, use_ml=use_ml)
        month_rows.append(row)
        state["months"] = month_rows
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        s = row["summary"]
        ml = row.get("summary_ml") or {}
        print(
            f"  [{label}] primary trades={s['trades']} pnl={s['pnl_usdt']:+.2f} "
            f"WR={s['win_rate']*100:.1f}% | raw_pnl={row['summary_raw']['pnl_usdt']:+.2f} "
            f"ml_pnl={ml.get('pnl_usdt', 0):+.2f} skipped={ml.get('ml_skipped', 0)} "
            f"elapsed={row['elapsed_sec']}s",
            flush=True,
        )

    overall = merge_months(month_rows, "summary")
    overall_raw = merge_months(month_rows, "summary_raw")
    overall_ml = merge_months(month_rows, "summary_ml")

    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "timerange": args.timerange,
        "pairs_source": args.pairs,
        "n_pairs": len(pairs),
        "ml_enabled": use_ml,
        "note": (
            "All real bots except stubs/grid (_sim_live missing). "
            f"ML gate {'ON' if use_ml else 'OFF'}. Scanner skipped (all pairs armed). "
            "Both raw and ml summaries stored for comparison."
        ),
        "overall": overall,
        "overall_raw": overall_raw,
        "overall_ml": overall_ml,
        "delta_ml_minus_raw": {
            "pnl_usdt": round(overall_ml["pnl_usdt"] - overall_raw["pnl_usdt"], 4),
            "trades": overall_ml["trades"] - overall_raw["trades"],
            "win_rate_pp": round((overall_ml["win_rate"] - overall_raw["win_rate"]) * 100, 2),
            "ml_skipped": overall_ml.get("ml_skipped"),
            "ml_skipped_pnl": overall_ml.get("ml_skipped_pnl"),
        },
        "months": [
            {
                "month": m["month"],
                "elapsed_sec": m["elapsed_sec"],
                "raw": {
                    "trades": m["summary_raw"]["trades"],
                    "pnl_usdt": m["summary_raw"]["pnl_usdt"],
                    "win_rate": m["summary_raw"]["win_rate"],
                },
                "ml": {
                    "trades": m["summary_ml"]["trades"],
                    "pnl_usdt": m["summary_ml"]["pnl_usdt"],
                    "win_rate": m["summary_ml"]["win_rate"],
                    "ml_skipped": m["summary_ml"].get("ml_skipped"),
                    "ml_skipped_pnl": m["summary_ml"].get("ml_skipped_pnl"),
                },
                "bots_ml": m["summary_ml"]["bots"],
                "bots_raw": m["summary_raw"]["bots"],
            }
            for m in month_rows
        ],
    }
    out_json = out_dir / "summary.json"
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_path = out_dir / "bots_summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "scenario_id", "label", "group",
                "raw_trades", "raw_pnl", "raw_wr",
                "ml_trades", "ml_pnl", "ml_wr",
                "ml_skipped", "ml_skipped_pnl", "delta_pnl",
            ],
        )
        w.writeheader()
        raw_by = {b["scenario_id"]: b for b in overall_raw["bots"]}
        for b in overall_ml["bots"]:
            r = raw_by.get(b["scenario_id"], {})
            w.writerow(
                {
                    "scenario_id": b["scenario_id"],
                    "label": b["label"],
                    "group": b["group"],
                    "raw_trades": r.get("trades"),
                    "raw_pnl": r.get("pnl_usdt"),
                    "raw_wr": r.get("win_rate"),
                    "ml_trades": b["trades"],
                    "ml_pnl": b["pnl_usdt"],
                    "ml_wr": b["win_rate"],
                    "ml_skipped": b.get("ml_skipped"),
                    "ml_skipped_pnl": b.get("ml_skipped_pnl"),
                    "delta_pnl": round(b["pnl_usdt"] - float(r.get("pnl_usdt") or 0), 4),
                }
            )

    print("\n=== OVERALL RAW vs ML ===", flush=True)
    print(
        f"  RAW  trades={overall_raw['trades']} pnl={overall_raw['pnl_usdt']:+.2f} "
        f"WR={overall_raw['win_rate']*100:.1f}%",
        flush=True,
    )
    print(
        f"  ML   trades={overall_ml['trades']} pnl={overall_ml['pnl_usdt']:+.2f} "
        f"WR={overall_ml['win_rate']*100:.1f}% skipped={overall_ml.get('ml_skipped')} "
        f"skipped_pnl={overall_ml.get('ml_skipped_pnl'):+.2f}",
        flush=True,
    )
    dlt = report["delta_ml_minus_raw"]
    print(
        f"  DELTA pnl={dlt['pnl_usdt']:+.2f} trades={dlt['trades']:+d} "
        f"WR={dlt['win_rate_pp']:+.2f}pp",
        flush=True,
    )
    print("\n  by bot (ML pnl):", flush=True)
    for b in overall_ml["bots"]:
        r = raw_by.get(b["scenario_id"], {})
        print(
            f"    {b['scenario_id']:18s} ml={b['pnl_usdt']:+8.2f} "
            f"raw={float(r.get('pnl_usdt') or 0):+8.2f} "
            f"d={b['pnl_usdt']-float(r.get('pnl_usdt') or 0):+7.2f} "
            f"n_ml={b['trades']:5d} skip={b.get('ml_skipped',0)}",
            flush=True,
        )
    print(f"\nWrote {out_json}", flush=True)
    print(f"Wrote {csv_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

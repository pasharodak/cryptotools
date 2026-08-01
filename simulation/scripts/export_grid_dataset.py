#!/usr/bin/env python3
"""Export live_grid sim trades — full-period backtest (fixes monthly scan reset)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import BotSessionManager  # noqa: E402
from simulation.exchange_sim.trade_archive import persist_session  # noqa: E402
from simulation.scripts.comparison_common import DEFAULT_TIMERANGE, PROD_PAIRS_PATH, pairs_from_source  # noqa: E402
from simulation.scripts.compare_ml_by_month import month_range  # noqa: E402
from simulation.scripts.run_full_ml_study import _save_trade_files, _trade_outcome  # noqa: E402

OUT_DIR = ROOT / "simulation/results/grid_ml/export"
STATE_PATH = ROOT / "simulation/results/grid_ml/export_state.json"
GRID_ID = "live_grid"
DEFAULT_SCAN_INTERVAL_MIN = 10


def timerange_to_ms(timerange: str) -> tuple[int, int]:
    start_s, end_s = timerange.split("-")
    y0, m0, d0 = int(start_s[:4]), int(start_s[4:6]), int(start_s[6:8] or 1)
    y1, m1, d1 = int(end_s[:4]), int(end_s[4:6]), int(end_s[6:8] or 1)
    start_ms, _, _ = month_range(y0, m0)
    _, end_ms, _ = month_range(y1, m1)
    if d1 != 1:
        from datetime import UTC, datetime

        end_ms = int(datetime(y1, m1, d1, 23, 59, 59, tzinfo=UTC).timestamp() * 1000)
    return start_ms, end_ms


def _trades_for_inst(inst: dict, trade_source: str) -> list[dict]:
    if trade_source == "raw":
        return list(inst.get("raw_trades") or [])
    if trade_source == "scanner":
        return list(inst.get("scanner_trades") or [])
    # both: scanner first, then raw not in scanner open_ms
    scanner = list(inst.get("scanner_trades") or [])
    scan_ms = {t.get("open_ms") for t in scanner}
    extra = [t for t in (inst.get("raw_trades") or []) if t.get("open_ms") not in scan_ms]
    return scanner + extra


def _instance_to_records(inst: dict, trades: list[dict], *, scan_interval_ms: int, trade_source: str) -> list[dict]:
    sc_id = inst.get("scenario_id") or GRID_ID
    cfg = inst.get("config") or {}
    records: list[dict] = []
    for t in trades:
        records.append(
            {
                "scenario_id": sc_id,
                "label": inst.get("label"),
                "strategy": inst.get("strategy"),
                "group": inst.get("group"),
                "pair": inst.get("pair"),
                "profit_abs": float(t.get("profit_abs") or 0),
                "profit_ratio": t.get("profit_ratio"),
                "trade": t,
                "inst_config": {
                    "stake": cfg.get("stake"),
                    "stoploss": cfg.get("stoploss"),
                    "minimal_roi": cfg.get("minimal_roi"),
                    "timeframe": cfg.get("timeframe", "5m"),
                },
                "armed_at_ms": inst.get("armed_at_ms"),
                "scan_type": inst.get("group"),
                "scan_interval_ms": scan_interval_ms,
                "trade_source": trade_source,
            }
        )
    return records


def export_full_period(
    pairs: list[str],
    timerange: str,
    *,
    scan_interval_ms: int,
    trade_source: str = "scanner",
    pool_mode: str = "direct",
    scan_workers: int | None = None,
) -> tuple[list[dict], dict[str, int]]:
    start_ms, end_ms = timerange_to_ms(timerange)
    datadir = ROOT / "simulation/data/ctengine"
    mgr = BotSessionManager(ROOT)
    mgr._get_ml_gate().set_enabled(False)
    mgr.init_live_session(
        pairs,
        start_ms,
        end_ms,
        datadir,
        scan_interval_ms=scan_interval_ms,
        pool_mode=pool_mode,
        scan_workers=scan_workers,
    )
    pool = list(pairs)
    mgr.load_scenario_instances(GRID_ID, pool, start_ms, end_ms, datadir)
    interval = int(mgr.scan_schedule.get("scan_interval_ms") or scan_interval_ms)

    records: list[dict] = []
    stats = {"raw": 0, "scanner": 0, "exported": 0, "pairs": len(pool), "instances": 0}
    for inst in mgr.instances:
        if inst.get("scenario_id") != GRID_ID:
            continue
        stats["instances"] += 1
        stats["raw"] += len(inst.get("raw_trades") or [])
        stats["scanner"] += len(inst.get("scanner_trades") or [])
        picked = _trades_for_inst(inst, trade_source)
        records.extend(_instance_to_records(inst, picked, scan_interval_ms=interval, trade_source=trade_source))

    stats["exported"] = len(records)
    persist_session(mgr, ROOT, source=f"grid_ml_full_{timerange}")
    return records, stats


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Export live_grid trades (full-period backtest)")
    ap.add_argument("--pairs", choices=("pool", "all", "prod200", "grid_priority"), default="prod200")
    ap.add_argument("--timerange", default=DEFAULT_TIMERANGE)
    ap.add_argument("--scan-interval-min", type=int, default=DEFAULT_SCAN_INTERVAL_MIN)
    ap.add_argument(
        "--trade-source",
        choices=("scanner", "raw", "both"),
        default="raw",
        help="raw=all backtest trades (prod VolumePairList); scanner=whitelist replay; both=union",
    )
    ap.add_argument(
        "--pool-mode",
        choices=("direct", "profile"),
        default=None,
        help="direct=use all requested pairs; profile=player pool intersection",
    )
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument(
        "--scan-workers",
        type=int,
        default=0,
        help="Parallel workers for scanner replay (0=auto cpu_count, max 16)",
    )
    args = ap.parse_args()

    if args.fresh and OUT_DIR.is_dir():
        import shutil

        shutil.rmtree(OUT_DIR)
    STATE_PATH.unlink(missing_ok=True)

    start_s = args.timerange.split("-")[0]
    if args.pairs in ("prod200", "prod"):
        path = ROOT / PROD_PAIRS_PATH
        pairs = list(json.loads(path.read_text(encoding="utf-8")).get("pairs") or [])
    else:
        pairs = pairs_from_source(ROOT, args.pairs, min_start=start_s[:8])
    scan_interval_ms = int(args.scan_interval_min) * 60 * 1000
    pool_mode = args.pool_mode or ("profile" if args.pairs == "pool" else "direct")

    print(
        f"=== GRID full export · {len(pairs)} pairs · {args.timerange} · "
        f"{args.scan_interval_min}m scan · source={args.trade_source} · pool={pool_mode} ===",
        flush=True,
    )
    records, stats = export_full_period(
        pairs,
        args.timerange,
        scan_interval_ms=scan_interval_ms,
        trade_source=args.trade_source,
        pool_mode=pool_mode,
        scan_workers=args.scan_workers or None,
    )
    _save_trade_files(OUT_DIR, records)
    w = sum(1 for r in records if _trade_outcome(r["trade"]) == "wins")
    state = {
        "mode": "full_period",
        "timerange": args.timerange,
        "pairs": args.pairs,
        "n_pairs": len(pairs),
        "scan_interval_min": args.scan_interval_min,
        "trade_source": args.trade_source,
        "total_trades": len(records),
        "wins": w,
        "losses": len(records) - w,
        **stats,
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"done: exported {len(records)} (raw {stats['raw']} scanner {stats['scanner']}) "
        f"· W{w} L{len(records) - w} -> {OUT_DIR}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Download OHLCV for extension pairs and compare scanner vs ML gate modes (OOS test)."""
from __future__ import annotations

import calendar
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import BotSessionManager, patch_whitelist  # noqa: E402
from simulation.exchange_sim.player_sync import summarize_trades  # noqa: E402

WORKERS = 3
TIMERANGE = "20251101-20260625"
MONTHS = [
    (2025, 11),
    (2025, 12),
    (2026, 1),
    (2026, 2),
    (2026, 3),
    (2026, 4),
    (2026, 5),
    (2026, 6),
]
GATE_MODES = {
    "block_loss": "block predicted loss only",
    "profit_only": "allow predicted profit only",
}


def extension_pairs(root: Path) -> list[str]:
    path = root / "simulation/config/extension_pairs.json"
    return json.loads(path.read_text(encoding="utf-8"))["pairs"]


def download_pairs(root: Path, pairs: list[str], timerange: str) -> int:
    datadir = root / "simulation/data/ctengine"
    runtime = root / "simulation/data/runtime/download_extension_pairs.json"
    patch_whitelist(root, "simulation/config/backtest_lite_base.json", pairs, runtime)
    ft = root / ".venv/Scripts/ctbot.exe"
    print(f"=== 5m download {timerange} · {len(pairs)} extension pairs ===")
    cmd = [
        str(ft),
        "download-data",
        "--config",
        str(runtime),
        "--datadir",
        str(datadir),
        "--timeframe",
        "5m",
        "--timerange",
        timerange,
        "--trading-mode",
        "futures",
        "--prepend",
    ]
    return subprocess.run(cmd, cwd=str(root)).returncode


def month_range(year: int, month: int) -> tuple[int, int, str]:
    last = calendar.monthrange(year, month)[1]
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year, month, last, 23, 59, 59, tzinfo=UTC)
    label = f"{year}-{month:02d}"
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), label


def aggregate_instances(instances: list[dict], *, use_ml: bool) -> dict:
    sum_key = "ml_summary" if use_ml else "scanner_summary"
    total_trades = 0
    total_pnl = 0.0
    wins = 0
    losses = 0
    for inst in instances:
        s = inst.get(sum_key) or summarize_trades(
            inst.get("scanner_trades" if not use_ml else inst.get("trades") or [])
        )
        total_trades += int(s.get("total_trades") or 0)
        total_pnl += float(s.get("profit_abs") or 0)
        wins += int(s.get("wins") or 0)
        losses += int(s.get("losses") or 0)
    skipped = sum(int((i.get("ml_gate") or {}).get("skipped") or 0) for i in instances)
    skipped_pnl = sum(
        float(t.get("profit_abs") or 0) for i in instances for t in i.get("ml_skipped_trades") or []
    )
    return {
        "trades": total_trades,
        "pnl_usdt": round(total_pnl, 4),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total_trades, 4) if total_trades else 0,
        "ml_skipped": skipped,
        "skipped_pnl_usdt": round(skipped_pnl, 4),
    }


def aggregate_by_pair(instances: list[dict]) -> list[dict]:
    by_pair: dict[str, dict] = {}
    for inst in instances:
        pair = inst["pair"]
        by_pair.setdefault(pair, {"pair": pair, "bots": 0})
        by_pair[pair]["bots"] += 1
    out = []
    for pair, row in by_pair.items():
        subset = [i for i in instances if i["pair"] == pair]
        wo = aggregate_instances(subset, use_ml=False)
        ml = aggregate_instances(subset, use_ml=True)
        delta = round(ml["pnl_usdt"] - wo["pnl_usdt"], 4)
        out.append(
            {
                **row,
                "without_ml": wo,
                "with_ml": ml,
                "delta_pnl_usdt": delta,
                "better": "ml" if delta > 0 else ("tie" if delta == 0 else "scanner"),
            }
        )
    return sorted(out, key=lambda r: r["delta_pnl_usdt"], reverse=True)


def pair_totals_from_rows(rows: list[dict]) -> list[dict]:
    acc_map: dict[str, dict] = {}
    for row in rows:
        for pr in row["by_pair"]:
            p = pr["pair"]
            acc = acc_map.setdefault(
                p, {"pair": p, "without_ml": 0.0, "with_ml": 0.0, "trades_no": 0, "trades_ml": 0, "skip": 0}
            )
            acc["without_ml"] += pr["without_ml"]["pnl_usdt"]
            acc["with_ml"] += pr["with_ml"]["pnl_usdt"]
            acc["trades_no"] += pr["without_ml"]["trades"]
            acc["trades_ml"] += pr["with_ml"]["trades"]
            acc["skip"] += pr["with_ml"]["ml_skipped"]
    out = []
    for acc in acc_map.values():
        d = round(acc["with_ml"] - acc["without_ml"], 4)
        out.append({**acc, "delta_pnl_usdt": d, "better": "ml" if d > 0 else ("tie" if d == 0 else "scanner")})
    return sorted(out, key=lambda r: r["delta_pnl_usdt"], reverse=True)


def configure_gate(mgr: BotSessionManager, gate_mode: str) -> None:
    mgr._ml_gate = None
    gate = mgr._get_ml_gate()
    gate.config["gate_mode"] = gate_mode
    gate.config["block_predicted"] = "loss"
    gate.set_enabled(False)


def run_month(pairs: list[str], year: int, month: int, workers: int, gate_mode: str) -> dict:
    start_ms, end_ms, label = month_range(year, month)
    datadir = ROOT / "simulation/data/ctengine"
    mgr = BotSessionManager(ROOT)
    configure_gate(mgr, gate_mode)

    mgr.init_live_session(pairs, start_ms, end_ms, datadir)
    pool = mgr.status.get("sim_pool") or pairs
    order = mgr.enabled_scenario_ids()

    def load_one(sid: str) -> str:
        mgr.load_scenario_instances(sid, pool, start_ms, end_ms, datadir)
        return sid

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(load_one, sid): sid for sid in order}
        for fut in as_completed(futs):
            fut.result()

    instances = list(mgr.instances)
    without_ml = aggregate_instances(instances, use_ml=False)
    with_ml = aggregate_instances(instances, use_ml=True)
    delta_pnl = round(with_ml["pnl_usdt"] - without_ml["pnl_usdt"], 4)
    return {
        "month": label,
        "gate_mode": gate_mode,
        "without_ml": without_ml,
        "with_ml": with_ml,
        "delta_pnl_usdt": delta_pnl,
        "better": "ml" if delta_pnl > 0 else ("tie" if delta_pnl == 0 else "scanner"),
        "by_pair": aggregate_by_pair(instances),
    }


def run_mode(pairs: list[str], gate_mode: str, workers: int) -> dict:
    label = GATE_MODES[gate_mode]
    print(f"\n=== {gate_mode}: {label} ===\n")
    print(f"{'Month':<8} {'Trades':>7} {'->ML':>6} {'PnL noML':>10} {'PnL ML':>10} {'Delta':>8} {'Skip':>5} Better")
    print("-" * 70)
    rows: list[dict] = []
    for y, m in MONTHS:
        print(f"  running {y}-{m:02d}...", flush=True)
        row = run_month(pairs, y, m, workers, gate_mode)
        rows.append(row)
        w, ml = row["without_ml"], row["with_ml"]
        print(
            f"{row['month']:<8} {w['trades']:>7} {ml['trades']:>6} "
            f"{w['pnl_usdt']:>10.2f} {ml['pnl_usdt']:>10.2f} "
            f"{row['delta_pnl_usdt']:>+8.2f} {ml['ml_skipped']:>5} {row['better']}"
        )
    total_wo = sum(r["without_ml"]["pnl_usdt"] for r in rows)
    total_ml = sum(r["with_ml"]["pnl_usdt"] for r in rows)
    total_skip = sum(r["with_ml"]["ml_skipped"] for r in rows)
    print("-" * 70)
    print(f"{'TOTAL':<8} {'':>7} {'':>6} {total_wo:>10.2f} {total_ml:>10.2f} {total_ml - total_wo:>+8.2f} {total_skip:>5}")
    pair_rows = pair_totals_from_rows(rows)
    print(f"\n--- by pair ({gate_mode}) ---")
    print(f"{'Pair':<22} {'Tr no':>6} {'Tr ML':>6} {'PnL no':>9} {'PnL ML':>9} {'Delta':>8} Better")
    print("-" * 70)
    for pr in pair_rows:
        print(
            f"{pr['pair']:<22} {pr['trades_no']:>6} {pr['trades_ml']:>6} "
            f"{pr['without_ml']:>9.2f} {pr['with_ml']:>9.2f} {pr['delta_pnl_usdt']:>+8.2f} {pr['better']}"
        )
    return {
        "gate_mode": gate_mode,
        "description": label,
        "months": rows,
        "pair_totals": pair_rows,
        "total_without_ml_usdt": round(total_wo, 4),
        "total_with_ml_usdt": round(total_ml, 4),
        "total_delta_usdt": round(total_ml - total_wo, 4),
        "total_skipped": total_skip,
    }


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="OOS ML test on extension pairs (multiple gate modes)")
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--timerange", default=TIMERANGE)
    ap.add_argument(
        "--gate-mode",
        choices=list(GATE_MODES),
        action="append",
        help="Run one mode (repeatable). Default: both block_loss and profit_only",
    )
    args = ap.parse_args()

    pairs = extension_pairs(ROOT)
    modes = args.gate_mode or list(GATE_MODES)
    print(f"Extension pairs ({len(pairs)}): {', '.join(pairs)}")

    if not args.skip_download:
        rc = download_pairs(ROOT, pairs, args.timerange)
        if rc != 0:
            print("WARN: download had errors — continuing with available data")

    results = [run_mode(pairs, mode, args.workers) for mode in modes]

    scanner_pnl = results[0]["total_without_ml_usdt"]
    print("\n=== COMPARISON ===")
    print(f"Scanner only: {scanner_pnl:+.2f} USDT")
    for r in results:
        print(
            f"  {r['gate_mode']:<12} {r['total_with_ml_usdt']:>+10.2f} USDT  "
            f"delta {r['total_delta_usdt']:>+8.2f}  skip {r['total_skipped']:>5}"
        )

    out = {
        "compared_at": datetime.now(tz=UTC).isoformat(),
        "model": "xgboost entry gate — OOS on extension pairs",
        "pairs": pairs,
        "timerange": args.timerange,
        "scanner_baseline_usdt": scanner_pnl,
        "modes": {r["gate_mode"]: r for r in results},
    }
    out_path = ROOT / "simulation/results/trade_db/models/ml_extension_pairs_gate_modes.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

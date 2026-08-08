#!/usr/bin/env python3
"""Download Mar–Apr data, run full-period prgon, fill trade_db, write report."""
from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import BotSessionManager  # noqa: E402
from simulation.exchange_sim.trade_archive import build_period_report, persist_session  # noqa: E402
from simulation.scripts.select_player_pairs import pool_pairs  # noqa: E402

FULL_TIMERANGE = "20260301-20260625"
MAR_APR_TIMERANGE = "20260301-20260430"
NOV_MAR_TIMERANGE = "20251101-20260331"
WORKERS = 3


def ms_range(timerange: str) -> tuple[int, int]:
    a, b = timerange.split("-")
    start = int(datetime.strptime(a, "%Y%m%d").replace(tzinfo=UTC).timestamp() * 1000)
    end = int(datetime.strptime(b, "%Y%m%d").replace(hour=23, minute=59, second=59, tzinfo=UTC).timestamp() * 1000)
    return start, end


def update_manifest(timerange: str) -> None:
    path = ROOT / "simulation/config/manifest.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["timerange"] = timerange
    cfg["player_timerange"] = timerange
    path.write_text(json.dumps(cfg, indent=4) + "\n", encoding="utf-8")
    print(f"manifest -> {timerange}")


def download_period(timerange: str, *, skip_1s: bool = False) -> int:
    update_manifest(timerange)
    print(f"=== download {timerange} ===")
    cmd = [
        str(ROOT / ".venv/Scripts/python.exe"),
        str(ROOT / "simulation/scripts/download_player_month.py"),
    ]
    if skip_1s:
        cmd.append("--skip-1s")
    r = subprocess.run(cmd, cwd=str(ROOT))
    return r.returncode


def run_prgon(timerange: str, workers: int = WORKERS) -> dict:
    start_ms, end_ms = ms_range(timerange)
    datadir = ROOT / "simulation/data/ctengine"
    pairs = pool_pairs(ROOT)
    mgr = BotSessionManager(ROOT)
    print(f"=== prgon {timerange} · {len(pairs)} pairs · {len(mgr.enabled_scenario_ids())} bots ===")
    mgr.init_live_session(pairs, start_ms, end_ms, datadir)
    pool = mgr.status.get("sim_pool") or pairs
    order = mgr.enabled_scenario_ids()

    def load_one(sid: str) -> tuple[str, str | None]:
        try:
            mgr.load_scenario_instances(sid, pool, start_ms, end_ms, datadir)
            n = sum(len(i.get("trades") or []) for i in mgr.instances if i["scenario_id"] == sid)
            return sid, f"{n} trades"
        except Exception as exc:
            return sid, f"ERROR: {exc}"

    with ThreadPoolExecutor(max_workers=workers) as pool_ex:
        futures = {pool_ex.submit(load_one, sid): sid for sid in order}
        for fut in as_completed(futures):
            sid, msg = fut.result()
            sc = next((s for s in mgr.scenarios if s["id"] == sid), {})
            print(f"  {sc.get('label', sid)}: {msg}")

    result = persist_session(mgr, ROOT, source="period_prgon")
    mgr.status["running"] = False
    mgr.status["phase"] = "ready"
    return result


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Fill trade_db for Mar–Jun period")
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--skip-1s", action="store_true", help="5m only when downloading")
    ap.add_argument("--download-timerange", default=None, help="Download range (default: --timerange)")
    ap.add_argument("--timerange", default=FULL_TIMERANGE)
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--train", action="store_true", help="Retrain PnL classifier after fill")
    args = ap.parse_args()

    dl_range = args.download_timerange or args.timerange

    if not args.skip_download:
        if download_period(dl_range, skip_1s=args.skip_1s) != 0:
            print("WARN: download had errors — continuing with available data")
        if dl_range != args.timerange:
            update_manifest(args.timerange)

    archive = run_prgon(args.timerange, workers=args.workers)
    db_root = ROOT / "simulation/results/trade_db"
    report = build_period_report(db_root)
    report_path = db_root / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== trade_db ===")
    print(f"  saved this run: {archive.get('trades_saved')} (+ skipped dup {archive.get('trades_skipped_duplicate')})")
    print(f"  profit: {archive.get('profit_trades')} trades · {archive.get('profit_usdt')} USDT")
    print(f"  loss:   {archive.get('loss_trades')} trades · {archive.get('loss_usdt')} USDT")
    print(f"  net:    {archive.get('net_usdt')} USDT")
    print(f"  unique in DB: {report.get('total_unique_trades')}")
    print(f"  report: {report_path}")

    if args.train:
        print("\n=== retrain PnL classifier ===")
        r = subprocess.run(
            [str(ROOT / ".venv/Scripts/python.exe"), str(ROOT / "simulation/scripts/train_pnl_classifier.py")],
            cwd=str(ROOT),
        )
        return r.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

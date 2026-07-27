#!/usr/bin/env python3
"""Portfolio backtest with scanner gating (no probe oracle) for a timerange."""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import (  # noqa: E402
    normalize_trade,
    patch_whitelist,
)
from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.exchange_sim.player_sync import (  # noqa: E402
    filter_trades_by_pair_pnl_history,
    filter_trades_rolling_whitelist,
    pair_ever_whitelisted,
    resolve_armed_at,
    summarize_trades,
)
from simulation.exchange_sim.scan_replay import build_arm_schedules  # noqa: E402
from simulation.scripts.select_player_pairs import pool_pairs  # noqa: E402


def ms_range(timerange: str) -> tuple[int, int]:
    a, b = timerange.split("-")
    start = int(datetime.strptime(a, "%Y%m%d").replace(tzinfo=UTC).timestamp() * 1000)
    end = int(datetime.strptime(b, "%Y%m%d").replace(hour=23, minute=59, second=59, tzinfo=UTC).timestamp() * 1000)
    return start, end


def run_bt(sc: dict, pairs: list[str], timerange: str) -> tuple[list[dict], dict]:
    runtime = ROOT / "simulation/data/runtime" / f"bench_scan_{sc['id']}.json"
    cfg = patch_whitelist(ROOT, sc["config"], pairs, runtime, scenario=sc)
    ft = ROOT / ".venv/Scripts/freqtrade.exe"
    out_dir = ROOT / "simulation/results/scanner_bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ft),
        "backtesting",
        "--config",
        str(runtime),
        "--strategy",
        sc["strategy"],
        "--strategy-path",
        str(ROOT / sc["strategy_path"]),
        "--datadir",
        str(ROOT / "simulation/data/freqtrade"),
        "--timerange",
        timerange,
        "--export",
        "trades",
        "--export-filename",
        f"scan_{sc['id']}",
        "--backtest-directory",
        str(out_dir),
        "--cache",
        "none",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    last_path = out_dir / ".last_result.json"
    if proc.returncode != 0 or not last_path.is_file():
        return [], cfg
    try:
        last = json.loads(last_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [], cfg
    zpath = out_dir / last["latest_backtest"]
    with zipfile.ZipFile(zpath) as zf:
        js = next(n for n in zf.namelist() if n.endswith(".json") and "_config" not in n)
        data = json.loads(zf.read(js))
    raw = data.get("strategy", {}).get(sc["strategy"], {}).get("trades") or []
    roi = cfg.get("minimal_roi") or {}
    out = []
    for t in raw:
        nt = normalize_trade(t, roi)
        nt["pair"] = t.get("pair", "")
        out.append(nt)
    return out, cfg


def eval_portfolio(timerange: str, wallet: float = 100.0) -> dict:
    scenarios = [
        s
        for s in json.loads((ROOT / "simulation/config/player_scenarios.json").read_text(encoding="utf-8"))
        if s.get("enabled", True)
    ]
    pairs = pool_pairs(ROOT)
    start_ms, end_ms = ms_range(timerange)
    datadir = ROOT / "simulation/data/freqtrade"
    ds = HistoricalDatastore(datadir)
    grid_bl: set[str] = set()

    strat_rows = []
    total_pnl = 0.0
    total_trades = 0

    trades_cache: dict[str, list] = {}

    for sc in scenarios:
        raw, cfg = run_bt(sc, pairs, timerange)
        trades_cache[sc["id"]] = raw
        if sc.get("scan_type") == "grid":
            grid_bl = set(cfg.get("exchange", {}).get("pair_blacklist") or [])

    grid_by_pair: dict[str, list] = {}
    for sc in scenarios:
        if sc.get("scan_type") == "grid" and sc["id"] in trades_cache:
            for t in trades_cache[sc["id"]]:
                grid_by_pair.setdefault(t["pair"], []).append(t)

    sched = build_arm_schedules(ds, pairs, start_ms, end_ms, ROOT, grid_bl, grid_trades_by_pair=grid_by_pair)

    rolling = sched.get("rolling_whitelist", True)
    grace_scans = int(sched.get("whitelist_grace_scans", 2))
    loss_streak = int(sched.get("trade_skip_loss_streak", 0))
    loss_window = int(sched.get("trade_loss_window_ms", 7 * 24 * 3600 * 1000))
    loss_min_cum = float(sched.get("trade_skip_min_cum_loss_usdt", 0.0))

    for sc in scenarios:
        raw = trades_cache[sc["id"]]
        timeline_key = "grid_timeline" if sc.get("scan_type") == "grid" else "strategy_timeline"
        timeline = sched.get(timeline_key) or []
        by_pair: dict[str, list] = defaultdict(list)
        for t in raw:
            by_pair[t["pair"]].append(t)

        armed_n = 0
        traded_n = 0
        scenario_trades: list[dict] = []
        pair_detail = []
        for pair in pairs:
            ptr = sorted(by_pair.get(pair, []), key=lambda x: x["open_ms"])
            if rolling:
                if not pair_ever_whitelisted(pair, timeline):
                    continue
                armed_n += 1
                gated = filter_trades_rolling_whitelist(ptr, timeline, grace_scans=grace_scans)
                if sc.get("scan_type") == "grid" and loss_streak > 0:
                    gated = filter_trades_by_pair_pnl_history(
                        gated,
                        window_ms=loss_window,
                        min_closed=loss_streak,
                        min_cum_loss_usdt=loss_min_cum,
                    )
            else:
                arm = sched["grid_arms" if sc.get("scan_type") == "grid" else "strategy_arms"].get(pair)
                if arm is None:
                    continue
                armed_n += 1
                from simulation.exchange_sim.player_sync import filter_trades_after_arm

                armed = resolve_armed_at(arm, ptr, start_ms, allow_trade_fallback=False)
                gated = filter_trades_after_arm(ptr, armed)
            if gated:
                traded_n += 1
                scenario_trades.extend(gated)
            if gated:
                s = summarize_trades(gated)
                pair_detail.append({"pair": pair.split("/")[0], **s})

        summary = summarize_trades(scenario_trades)
        total_pnl += summary["profit_abs"]
        total_trades += summary["total_trades"]
        strat_rows.append(
            {
                "id": sc["id"],
                "label": sc["label"],
                "armed_pairs": armed_n,
                "traded_pairs": traded_n,
                "stake_usdt": sc.get("stake_usdt"),
                **summary,
                "top_pairs": sorted(pair_detail, key=lambda x: -x["profit_abs"])[:8],
            }
        )

    return {
        "timerange": timerange,
        "wallet_usdt": wallet,
        "pnl_usdt": round(total_pnl, 4),
        "pnl_pct": round(total_pnl / wallet * 100, 2) if wallet else 0,
        "total_trades": total_trades,
        "strategies": strat_rows,
    }


def main() -> int:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--timerange", default=None)
    p.add_argument("--out", default=None)
    args = p.parse_args()
    tr = args.timerange
    if not tr:
        tr = json.loads((ROOT / "simulation/config/manifest.json").read_text())["player_timerange"]
    report = eval_portfolio(tr)
    out = Path(args.out) if args.out else ROOT / "simulation/results/scanner_portfolio.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Collect top-30 strategy trades across a fixed SL/TP percent grid.

Levels default: 3, 8, 13 (% of price) — from 3%% step 5%% while <= 13%%.
Every (tp_pct, sl_pct) pair is backtested separately (full ctengine), train then test.

Supports --workers N (ProcessPoolExecutor). Cache is resume-safe.

Cache:
  simulation/results/ml_param_experiments/trade_cache_sltp/
    {scenario}__sl{SL}__tp{TP}__train.json
    {scenario}__sl{SL}__tp{TP}__test.json
  manifest.json — progress + raw trade stats

Split: train 20250101-20260331 · test 20260401-20260625
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SIM_SKIP_PERSIST", "1")

from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402
from simulation.scripts.run_ml_param_experiments import (  # noqa: E402
    DEFAULT_TEST,
    DEFAULT_TRAIN,
    load_top30_scenarios,
)
from simulation.scripts.run_scalp_strategies_compare import (  # noqa: E402
    collect_train_test_trades,
    summarize,
)

DEFAULT_CACHE = ROOT / "simulation/results/ml_param_experiments/trade_cache_sltp"
DEFAULT_OUT = ROOT / "simulation/results/ml_param_experiments/sltp_grid"


def pct_levels(start: float = 3.0, step: float = 5.0, stop: float = 13.0) -> list[float]:
    out: list[float] = []
    x = float(start)
    while x <= stop + 1e-9:
        out.append(round(x, 4))
        x += step
    return out


def sltp_grid(levels: list[float]) -> list[tuple[float, float]]:
    return [(tp, sl) for tp in levels for sl in levels]


def tag_pct(pct: float) -> str:
    if abs(pct - round(pct)) < 1e-9:
        return f"{int(round(pct)):02d}"
    return f"{pct:.2f}".replace(".", "p")


def cache_paths(cache_dir: Path, sid: str, tp_pct: float, sl_pct: float) -> tuple[Path, Path]:
    key = f"{sid}__sl{tag_pct(sl_pct)}__tp{tag_pct(tp_pct)}"
    return cache_dir / f"{key}__train.json", cache_dir / f"{key}__test.json"


def job_key(sid: str, tp: float, sl: float) -> tuple[str, float, float]:
    return (sid, float(tp), float(sl))


def load_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def patch_scenario_risk(mgr: Any, sid: str, *, tp_pct: float, sl_pct: float) -> dict[str, Any] | None:
    sc = next((s for s in mgr.scenarios if s.get("id") == sid), None)
    if sc is None:
        return None
    prev = {
        "stoploss": sc.get("stoploss"),
        "minimal_roi": sc.get("minimal_roi"),
        "tp": sc.get("tp"),
    }
    sc["stoploss"] = -abs(float(sl_pct) / 100.0)
    sc["minimal_roi"] = {"0": abs(float(tp_pct) / 100.0)}
    sc["tp"] = abs(float(tp_pct) / 100.0)
    return prev


def restore_scenario_risk(mgr: Any, sid: str, prev: dict[str, Any] | None) -> None:
    if prev is None:
        return
    sc = next((s for s in mgr.scenarios if s.get("id") == sid), None)
    if sc is None:
        return
    for k, v in prev.items():
        if v is None:
            sc.pop(k, None)
        else:
            sc[k] = v


def make_mgr() -> Any:
    from simulation.exchange_sim.bot_session import BotSessionManager

    mgr = BotSessionManager(ROOT)
    try:
        mgr._get_ml_gate().set_enabled(False)
    except Exception:
        pass
    mgr.scan_schedule = {"rolling_whitelist": False}
    return mgr


def summarize_cached(tr_path: Path, te_path: Path, sid: str, tp: float, sl: float) -> dict[str, Any] | None:
    train_tr = load_json(tr_path)
    test_tr = load_json(te_path)
    if not isinstance(train_tr, list) or not isinstance(test_tr, list):
        return None
    return {
        "scenario_id": sid,
        "tp_pct": tp,
        "sl_pct": sl,
        "cache_hit": True,
        "n_train": len(train_tr),
        "n_test": len(test_tr),
        "train": summarize(train_tr),
        "test": summarize(test_tr),
        "train_path": str(tr_path),
        "test_path": str(te_path),
    }


def collect_one(
    mgr: Any,
    *,
    sid: str,
    pairs: list[str],
    datadir: Path,
    train_range: str,
    test_range: str,
    tp_pct: float,
    sl_pct: float,
    cache_dir: Path,
    force: bool = False,
) -> dict[str, Any]:
    tr_path, te_path = cache_paths(cache_dir, sid, tp_pct, sl_pct)
    if not force:
        hit = summarize_cached(tr_path, te_path, sid, tp_pct, sl_pct)
        if hit is not None:
            return hit

    prev = patch_scenario_risk(mgr, sid, tp_pct=tp_pct, sl_pct=sl_pct)
    if prev is None:
        return {
            "scenario_id": sid,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "error": f"scenario not found: {sid}",
        }
    t0 = time.time()
    isolate = f"sl{tag_pct(sl_pct)}_tp{tag_pct(tp_pct)}"
    try:
        train_tr, test_tr = collect_train_test_trades(
            mgr,
            sid,
            pairs,
            datadir,
            train_range=train_range,
            test_range=test_range,
            isolate_key=isolate,
        )
        save_json(tr_path, train_tr)
        save_json(te_path, test_tr)
        elapsed = round(time.time() - t0, 1)
        return {
            "scenario_id": sid,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "cache_hit": False,
            "n_train": len(train_tr),
            "n_test": len(test_tr),
            "train": summarize(train_tr),
            "test": summarize(test_tr),
            "elapsed_sec": elapsed,
            "train_path": str(tr_path),
            "test_path": str(te_path),
            "stoploss": -abs(sl_pct) / 100.0,
            "minimal_roi": {"0": abs(tp_pct) / 100.0},
            "worker_pid": os.getpid(),
        }
    except Exception as exc:
        return {
            "scenario_id": sid,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "error": str(exc),
            "traceback": traceback.format_exc()[-2000:],
            "elapsed_sec": round(time.time() - t0, 1),
            "worker_pid": os.getpid(),
        }
    finally:
        restore_scenario_risk(mgr, sid, prev)


_WORKER_MGR: Any = None


def _init_worker() -> None:
    """Create one BotSessionManager per process (reuse across jobs)."""
    global _WORKER_MGR
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ.setdefault("SIM_SKIP_PERSIST", "1")
    _WORKER_MGR = make_mgr()


def _worker_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Picklable entry for ProcessPoolExecutor (one job per call)."""
    global _WORKER_MGR
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ.setdefault("SIM_SKIP_PERSIST", "1")

    sid = payload["sid"]
    tp = float(payload["tp"])
    sl = float(payload["sl"])
    cache_dir = Path(payload["cache_dir"])
    tr_path, te_path = cache_paths(cache_dir, sid, tp, sl)
    if not payload.get("force"):
        hit = summarize_cached(tr_path, te_path, sid, tp, sl)
        if hit is not None:
            return hit

    if _WORKER_MGR is None:
        _WORKER_MGR = make_mgr()
    return collect_one(
        _WORKER_MGR,
        sid=sid,
        pairs=list(payload["pairs"]),
        datadir=Path(payload["datadir"]),
        train_range=payload["train_range"],
        test_range=payload["test_range"],
        tp_pct=tp,
        sl_pct=sl,
        cache_dir=cache_dir,
        force=bool(payload.get("force")),
    )


def _print_row(i: int, total: int, row: dict[str, Any]) -> None:
    sid = row.get("scenario_id")
    tp = row.get("tp_pct")
    sl = row.get("sl_pct")
    prefix = f"[{i}/{total}] {sid} TP={tp:g}% SL={sl:g}%"
    if row.get("error"):
        print(f"{prefix} ERROR: {row['error']}", flush=True)
        return
    te = row.get("test") or {}
    if row.get("cache_hit"):
        print(
            f"{prefix} cache hit train={row.get('n_train')} test={row.get('n_test')} "
            f"WR={te.get('winrate')} pnl={te.get('pnl')}",
            flush=True,
        )
        return
    print(
        f"{prefix} ok train={row.get('n_train')} test={row.get('n_test')} "
        f"W/L={te.get('wins')}/{te.get('losses')} WR={te.get('winrate')} "
        f"pnl={te.get('pnl')} ({row.get('elapsed_sec')}s) pid={row.get('worker_pid')}",
        flush=True,
    )


def build_manifest(
    *,
    levels: list[float],
    pairs_tp_sl: list[tuple[float, float]],
    scenarios: list[str],
    pairs: list[str],
    train_range: str,
    test_range: str,
    workers: int,
    results: list[dict[str, Any]],
    cache_hits: int,
    backtested: int,
    errors: int,
    total: int,
    elapsed: float,
    status: str,
) -> dict[str, Any]:
    return {
        "updated": datetime.now(tz=UTC).isoformat(),
        "train_range": train_range,
        "test_range": test_range,
        "levels_pct": levels,
        "n_combos": len(pairs_tp_sl),
        "n_scenarios": len(scenarios),
        "n_jobs": total,
        "n_pairs": len(pairs),
        "workers": workers,
        "progress": {
            "completed_rows": len(results),
            "cache_hits": cache_hits,
            "backtested": backtested,
            "errors": errors,
            "elapsed_sec": round(elapsed, 1),
            "status": status,
        },
        "scenarios": scenarios,
        "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Collect top-30 trades over SL/TP percent grid")
    ap.add_argument("--train-range", default=DEFAULT_TRAIN)
    ap.add_argument("--test-range", default=DEFAULT_TEST)
    ap.add_argument("--pairs", default="all", help="all|prod200|pool")
    ap.add_argument("--max-pairs", type=int, default=0)
    ap.add_argument("--scenarios", default="top30", help="top30 | comma ids")
    ap.add_argument("--start-pct", type=float, default=3.0)
    ap.add_argument("--step-pct", type=float, default=5.0)
    ap.add_argument("--stop-pct", type=float, default=13.0)
    ap.add_argument("--levels", default="", help="override levels, e.g. 3,8,13")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="max jobs needing backtest this run (0=all)")
    ap.add_argument("--only-symmetric", action="store_true")
    ap.add_argument(
        "--workers",
        type=int,
        default=4,
        help="parallel process workers (1 = sequential). Default 4.",
    )
    args = ap.parse_args()

    if args.levels.strip():
        levels = [float(x.strip()) for x in args.levels.split(",") if x.strip()]
    else:
        levels = pct_levels(args.start_pct, args.step_pct, args.stop_pct)
    pairs_tp_sl = sltp_grid(levels)
    if args.only_symmetric:
        pairs_tp_sl = [(tp, sl) for tp, sl in pairs_tp_sl if abs(tp - sl) < 1e-9]

    scen_arg = args.scenarios.strip().lower()
    if scen_arg == "top30":
        scenarios = load_top30_scenarios()
    else:
        scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]

    pairs = pairs_from_source(ROOT, args.pairs, min_start="2025-01-01")
    pairs = [p for p in pairs if not p.startswith("XRP/")]
    if args.max_pairs and args.max_pairs > 0:
        pairs = pairs[: args.max_pairs]

    cache_dir = Path(args.cache_dir)
    out_dir = Path(args.out_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"

    # Round-robin by risk cell then scenario so parallel workers hit different strategies.
    jobs = [(sid, tp, sl) for tp, sl in pairs_tp_sl for sid in scenarios]
    total = len(jobs)
    workers = max(1, int(args.workers))

    # Split: cache hits stay in main; only missing need workers
    to_run: list[tuple[str, float, float]] = []
    results_map: dict[tuple[str, float, float], dict[str, Any]] = {}
    cache_hits = 0
    for sid, tp, sl in jobs:
        tr_path, te_path = cache_paths(cache_dir, sid, tp, sl)
        if not args.force:
            hit = summarize_cached(tr_path, te_path, sid, tp, sl)
            if hit is not None:
                results_map[job_key(sid, tp, sl)] = hit
                cache_hits += 1
                continue
        to_run.append((sid, tp, sl))

    if args.limit and args.limit > 0:
        to_run = to_run[: args.limit]

    eta_jobs = len(to_run)
    eta_h_lo = eta_jobs * 7 / 60 / workers
    eta_h_hi = eta_jobs * 10 / 60 / workers
    print(
        f"=== SL/TP GRID COLLECT · scenarios={len(scenarios)} · levels={levels} · "
        f"combos={len(pairs_tp_sl)} · jobs={total} · need_backtest={eta_jobs} · "
        f"cache_hits={cache_hits} · workers={workers} · pairs={len(pairs)} · "
        f"train={args.train_range} test={args.test_range} ===",
        flush=True,
    )
    print(f"cache={cache_dir}", flush=True)
    print(f"ETA rough: ~{eta_h_lo:.1f}–{eta_h_hi:.1f} hours wall ({workers} workers)", flush=True)

    datadir = ROOT / "simulation/data/ctengine"
    t_all = time.time()
    backtested = 0
    errors = 0
    completed = 0

    def flush_manifest(status: str) -> None:
        ordered = [
            results_map[job_key(sid, tp, sl)]
            for sid, tp, sl in jobs
            if job_key(sid, tp, sl) in results_map
        ]
        save_json(
            manifest_path,
            build_manifest(
                levels=levels,
                pairs_tp_sl=pairs_tp_sl,
                scenarios=scenarios,
                pairs=pairs,
                train_range=args.train_range,
                test_range=args.test_range,
                workers=workers,
                results=ordered,
                cache_hits=cache_hits,
                backtested=backtested,
                errors=errors,
                total=total,
                elapsed=time.time() - t_all,
                status=status,
            ),
        )

    flush_manifest("running")

    if not to_run:
        print("Nothing to backtest (all cache hits).", flush=True)
    elif workers == 1:
        mgr = make_mgr()
        for i, (sid, tp, sl) in enumerate(to_run, 1):
            row = collect_one(
                mgr,
                sid=sid,
                pairs=pairs,
                datadir=datadir,
                train_range=args.train_range,
                test_range=args.test_range,
                tp_pct=tp,
                sl_pct=sl,
                cache_dir=cache_dir,
                force=args.force,
            )
            results_map[job_key(sid, tp, sl)] = row
            completed += 1
            if row.get("error"):
                errors += 1
            elif not row.get("cache_hit"):
                backtested += 1
            _print_row(completed, eta_jobs, row)
            flush_manifest("running")
    else:
        payloads = [
            {
                "sid": sid,
                "tp": tp,
                "sl": sl,
                "pairs": pairs,
                "datadir": str(datadir),
                "train_range": args.train_range,
                "test_range": args.test_range,
                "cache_dir": str(cache_dir),
                "force": bool(args.force),
            }
            for sid, tp, sl in to_run
        ]
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
        ) as ex:
            futs = {ex.submit(_worker_job, p): p for p in payloads}
            for fut in as_completed(futs):
                p = futs[fut]
                try:
                    row = fut.result()
                except Exception as exc:
                    row = {
                        "scenario_id": p["sid"],
                        "tp_pct": p["tp"],
                        "sl_pct": p["sl"],
                        "error": f"worker crashed: {exc}",
                        "traceback": traceback.format_exc()[-2000:],
                    }
                results_map[job_key(p["sid"], p["tp"], p["sl"])] = row
                completed += 1
                if row.get("error"):
                    errors += 1
                elif not row.get("cache_hit"):
                    backtested += 1
                _print_row(completed, eta_jobs, row)
                flush_manifest("running")

    elapsed = time.time() - t_all
    ordered = [
        results_map[job_key(sid, tp, sl)]
        for sid, tp, sl in jobs
        if job_key(sid, tp, sl) in results_map
    ]
    ok = [r for r in ordered if not r.get("error") and r.get("test")]
    status = "complete" if len(ok) >= total else "partial"
    flush_manifest(status)

    ranked = sorted(
        ok,
        key=lambda r: float((r.get("test") or {}).get("pnl") or -1e18),
        reverse=True,
    )
    top_path = out_dir / "raw_test_pnl_top.json"
    save_json(
        top_path,
        {
            "updated": datetime.now(tz=UTC).isoformat(),
            "top": [
                {
                    "scenario_id": r["scenario_id"],
                    "tp_pct": r["tp_pct"],
                    "sl_pct": r["sl_pct"],
                    "test": r.get("test"),
                    "train": r.get("train"),
                    "n_train": r.get("n_train"),
                    "n_test": r.get("n_test"),
                }
                for r in ranked[:100]
            ],
        },
    )

    print(
        f"\nDone rows={len(ordered)} ok={len(ok)}/{total} cache_hits={cache_hits} "
        f"backtested={backtested} errors={errors} elapsed={elapsed:.0f}s workers={workers}",
        flush=True,
    )
    print(f"manifest={manifest_path}", flush=True)
    if ranked:
        best = ranked[0]
        te = best.get("test") or {}
        print(
            f"best raw test: {best['scenario_id']} TP={best['tp_pct']}% SL={best['sl_pct']}% "
            f"pnl={te.get('pnl')} WR={te.get('winrate')} n={te.get('n')}",
            flush=True,
        )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    # Required on Windows for ProcessPoolExecutor spawn
    raise SystemExit(main())

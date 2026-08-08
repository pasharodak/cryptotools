#!/usr/bin/env python3
"""
Full ML study pipeline:
  1. export  — run scanner on all pairs, full timerange; save wins/losses + by strategy
  2. train    — global model + per-scenario models
  3. compare  — no ML vs global ML vs per-strategy ML (profit ≥80%)
  4. all      — export → train → compare
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import BotSessionManager  # noqa: E402
from simulation.exchange_sim.trade_archive import persist_session  # noqa: E402
from simulation.ml.pnl_classifier import (  # noqa: E402
    load_all_training_trades,
    train_model,
    train_model_on_trades,
)
from simulation.ml.per_strategy_models import MIN_TRADES, train_all_scenario_models  # noqa: E402
from simulation.ml.trade_gate import MlEntryGate  # noqa: E402
from simulation.scripts.comparison_common import (  # noqa: E402
    DEFAULT_TIMERANGE,
    PROD_PAIRS_PATH,
    pairs_from_source,
    period_months,
)
from simulation.scripts.compare_ml_by_month import month_range  # noqa: E402

STUDY_DIR = ROOT / "simulation/results/full_ml_study"
EXPORT_DIR = STUDY_DIR / "export"
PROD_CFG = ROOT / "simulation/config/prod_ml_bots.json"
STATE_PATH = STUDY_DIR / "export_state.json"
COMPARE_PATH = STUDY_DIR / "comparison.json"
MODEL_DIR = ROOT / "simulation/results/trade_db/models"


def _safe_name(s: str) -> str:
    return (s or "unknown").replace("/", "_").replace(":", "_")


def _trade_outcome(t: dict) -> str:
    return "wins" if float(t.get("profit_abs") or 0) >= 0 else "losses"


def _instance_to_records(inst: dict) -> list[dict[str, Any]]:
    sc_id = inst.get("scenario_id") or ""
    cfg = inst.get("config") or {}
    records = []
    for t in inst.get("scanner_trades") or []:
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
            }
        )
    return records


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _save_trade_files(base: Path, records: list[dict]) -> None:
    by_outcome: dict[str, list[dict]] = defaultdict(list)
    by_strat: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for rec in records:
        outcome = _trade_outcome(rec["trade"])
        by_outcome[outcome].append(rec)
        by_strat[rec["scenario_id"]][outcome].append(rec)

    for outcome, rows in by_outcome.items():
        _append_jsonl(base / f"all_{outcome}.jsonl", rows)
        for i, rec in enumerate(rows):
            fp = base / outcome / f"{rec['scenario_id']}_{_safe_name(rec['pair'])}_{rec['trade'].get('open_ms')}_{i}.json"
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")

    for sid, outcomes in by_strat.items():
        for outcome, rows in outcomes.items():
            _append_jsonl(base / "by_strategy" / sid / f"{outcome}.jsonl", rows)


def _load_state() -> dict[str, Any]:
    if STATE_PATH.is_file():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"completed_months": [], "total_trades": 0, "wins": 0, "losses": 0}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def run_export_month(
    pairs: list[str],
    year: int,
    month: int,
    workers: int,
    enabled: set[str],
    *,
    pool_mode: str = "profile",
    scan_workers: int | None = None,
) -> tuple[str, list[dict], dict]:
    start_ms, end_ms, label = month_range(year, month)
    datadir = ROOT / "simulation/data/ctengine"
    mgr = BotSessionManager(ROOT)
    mgr._get_ml_gate().set_enabled(False)
    mgr.init_live_session(
        pairs, start_ms, end_ms, datadir, pool_mode=pool_mode, scan_workers=scan_workers
    )
    pool = list(pairs) if pool_mode == "direct" else (mgr.status.get("sim_pool") or pairs)
    order = [s for s in mgr.enabled_scenario_ids() if s in enabled]

    def load_one(sid: str) -> str:
        mgr.load_scenario_instances(sid, pool, start_ms, end_ms, datadir)
        return sid

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed({ex.submit(load_one, sid): sid for sid in order}):
            fut.result()

    instances = [i for i in mgr.instances if i.get("scenario_id") in enabled]
    records: list[dict] = []
    for inst in instances:
        records.extend(_instance_to_records(inst))

    archive = persist_session(mgr, ROOT, source=f"full_ml_study_{label}")
    return label, records, archive


def export_from_trade_db(enabled: set[str] | None = None) -> int:
    """Bootstrap export JSONL from existing trade_db (pool pairs, prior runs)."""
    from simulation.ml.pnl_classifier import load_trades

    db_root = ROOT / "simulation/results/trade_db"
    trades = load_trades(db_root)
    records: list[dict] = []
    for t in trades:
        sid = t.get("scenario_id") or (t.get("basis") or {}).get("scenario_id")
        if enabled and sid not in enabled:
            continue
        tr = t.get("trade") or {}
        basis = t.get("basis") or {}
        records.append(
            {
                "scenario_id": sid,
                "label": t.get("label"),
                "strategy": basis.get("strategy"),
                "group": basis.get("group"),
                "pair": t.get("pair"),
                "profit_abs": float(t.get("profit_abs") or 0),
                "profit_ratio": tr.get("profit_ratio"),
                "trade": tr,
                "inst_config": {
                    "stake": basis.get("stake_usdt"),
                    "stoploss": basis.get("stoploss"),
                    "minimal_roi": basis.get("minimal_roi"),
                    "timeframe": basis.get("timeframe", "5m"),
                },
                "armed_at_ms": basis.get("armed_at_ms"),
                "scan_type": basis.get("scan_type"),
            }
        )
    if EXPORT_DIR.is_dir() and any(EXPORT_DIR.glob("all_*.jsonl")):
        print(f"  export jsonl already present — skip bootstrap ({len(records)} in db)")
        return len(_load_exported_trades())
    _save_trade_files(EXPORT_DIR, records)
    w = sum(1 for r in records if _trade_outcome(r["trade"]) == "wins")
    print(f"  bootstrapped {len(records)} trades from trade_db · W{w} L{len(records)-w}")
    return len(records)


def _count_by_strategy_from_export() -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0})
    for rec in _load_exported_trades():
        sid = rec.get("scenario_id") or "unknown"
        counts[sid][_trade_outcome(rec["trade"])] += 1
    return dict(counts)


def phase_export(args) -> dict[str, Any]:
    prod = json.loads(PROD_CFG.read_text(encoding="utf-8"))
    enabled = set(prod["enabled_scenarios"])
    timerange = args.timerange or DEFAULT_TIMERANGE
    start_s, end_s = timerange.split("-")
    y0, m0 = int(start_s[:4]), int(start_s[4:6])
    y1, m1 = int(end_s[:4]), int(end_s[4:6])

    if args.pairs in ("prod200", "prod"):
        pairs = list(json.loads((ROOT / PROD_PAIRS_PATH).read_text(encoding="utf-8")).get("pairs") or [])
    else:
        pairs = pairs_from_source(ROOT, args.pairs, min_start=start_s[:8])
    pool_mode = "direct" if args.pairs in ("prod200", "prod", "all") else "profile"
    print(f"=== EXPORT · {len(pairs)} pairs · {timerange} · {len(enabled)} bots · pool={pool_mode} ===")

    if args.fresh:
        import shutil

        if EXPORT_DIR.is_dir():
            shutil.rmtree(EXPORT_DIR)
        STATE_PATH.unlink(missing_ok=True)

    state = _load_state()
    months = period_months(y0, m0, y1, m1)

    for y, m in months:
        label = f"{y}-{m:02d}"
        if label in state.get("completed_months", []) and not args.rerun_month:
            print(f"  skip {label}", flush=True)
            continue
        print(f"  export {label} ({len(pairs)} pairs)...", flush=True)
        _, records, archive = run_export_month(
            pairs, y, m, args.workers, enabled, pool_mode=pool_mode, scan_workers=args.scan_workers
        )
        _save_trade_files(EXPORT_DIR, records)

        w = sum(1 for r in records if _trade_outcome(r["trade"]) == "wins")
        l = len(records) - w
        state["total_trades"] = state.get("total_trades", 0) + len(records)
        state["wins"] = state.get("wins", 0) + w
        state["losses"] = state.get("losses", 0) + l
        state.setdefault("completed_months", []).append(label)
        state["last_archive"] = archive
        _save_state(state)
        print(f"    {len(records)} trades · W{w} L{l}", flush=True)

    index = {
        "exported_at": datetime.now(tz=UTC).isoformat(),
        "timerange": timerange,
        "pairs": len(pairs),
        "pairs_mode": args.pairs,
        "bots": sorted(enabled),
        "total_trades": state.get("total_trades", 0),
        "wins": state.get("wins", 0),
        "losses": state.get("losses", 0),
        "by_strategy": _count_by_strategy_from_export(),
        "paths": {
            "all_wins": str(EXPORT_DIR / "all_wins.jsonl"),
            "all_losses": str(EXPORT_DIR / "all_losses.jsonl"),
            "by_strategy": str(EXPORT_DIR / "by_strategy"),
            "wins_dir": str(EXPORT_DIR / "wins"),
            "losses_dir": str(EXPORT_DIR / "losses"),
        },
    }
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    (EXPORT_DIR / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nExport index: {EXPORT_DIR / 'index.json'}")
    print(f"  total: {index['total_trades']} · wins {index['wins']} · losses {index['losses']}")
    return index


def _load_exported_trades() -> list[dict]:
    rows: list[dict] = []
    for name in ("all_wins.jsonl", "all_losses.jsonl"):
        path = EXPORT_DIR / name
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def phase_train(args) -> dict[str, Any]:
    prod = json.loads(PROD_CFG.read_text(encoding="utf-8"))
    enabled = list(prod["enabled_scenarios"])
    source = getattr(args, "train_source", "trade_db")

    if source == "all":
        trades = load_all_training_trades(ROOT, EXPORT_DIR)
        print(f"=== TRAIN global model (all data: {len(trades)} trades) ===")
        prev_model = MODEL_DIR / "pnl_classifier.joblib"
        prev_meta = MODEL_DIR / "pnl_classifier_meta.json"
        if prev_model.is_file():
            import shutil

            shutil.copy2(prev_model, MODEL_DIR / "pnl_classifier_prev.joblib")
            if prev_meta.is_file():
                shutil.copy2(prev_meta, MODEL_DIR / "pnl_classifier_meta_prev.json")
            print("  backed up previous model -> pnl_classifier_prev.joblib")
        global_meta = train_model_on_trades(
            ROOT,
            trades,
            args.model,
            train_source="all",
            note="trade_db + full_ml_study export merged; time-based split",
        )
    elif source == "export":
        from simulation.ml.pnl_classifier import load_trades_from_export_dir

        trades = load_trades_from_export_dir(EXPORT_DIR)
        print(f"=== TRAIN global model (export only: {len(trades)} trades) ===")
        global_meta = train_model_on_trades(
            ROOT,
            trades,
            args.model,
            train_source="export",
            note="full_ml_study export only; time-based split",
        )
    else:
        print("=== TRAIN global model (trade_db) ===")
        global_meta = train_model(ROOT, model_name=args.model)

    print(f"  global: {global_meta['n_trades']} trades · acc {global_meta['accuracy']:.1%}")
    per_meta = {"trained": [], "skipped": enabled, "note": "skipped"}
    if not getattr(args, "skip_per_strategy", False):
        print("=== TRAIN per-scenario models ===")
        per_meta = train_all_scenario_models(
            ROOT,
            enabled,
            model_name=args.model,
            export_dir=EXPORT_DIR if EXPORT_DIR.is_dir() else None,
            min_trades=int(getattr(args, "min_trades_per_strategy", MIN_TRADES)),
        )
        print(f"  trained: {per_meta['trained']}")
        print(f"  skipped: {per_meta['skipped']}")
    out = {"global": global_meta, "per_scenario": per_meta}
    (STUDY_DIR / "training.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def _scenario_dict(rec: dict) -> dict:
    return {
        "id": rec["scenario_id"],
        "scenario_id": rec["scenario_id"],
        "label": rec.get("label"),
        "scan_type": rec.get("scan_type") or "strategy",
        "group": rec.get("group") or "unknown",
        "strategy": rec.get("strategy") or "unknown",
    }


def _aggregate_mode(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"trades": 0, "pnl_usdt": 0.0, "wins": 0, "losses": 0, "win_rate": 0.0}
    pnl = sum(float(r.get("profit_abs") or 0) for r in rows)
    wins = sum(1 for r in rows if float(r.get("profit_abs") or 0) >= 0)
    n = len(rows)
    return {
        "trades": n,
        "pnl_usdt": round(pnl, 4),
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n, 4) if n else 0.0,
    }


def _open_ms(rec: dict) -> int:
    tr = rec.get("trade") or {}
    return int(tr.get("open_ms") or 0)


def _oos_records(records: list[dict], test_frac: float = 0.2) -> list[dict]:
    """Same 80/20 time split as train_model_on_trades — newer trades = OOS test."""
    keyed = sorted(enumerate(records), key=lambda ir: (_open_ms(ir[1]), ir[0]))
    cut = int(len(keyed) * (1 - test_frac))
    return [r for _, r in keyed[cut:]]


def _gate_filter_records(
    records: list[dict],
    *,
    model_mode: str | None,
    gate_cfg: dict[str, Any],
) -> tuple[list[dict], list[dict]]:
    if model_mode is None:
        return list(records), []
    gate = MlEntryGate(ROOT, model_mode=model_mode)
    gate.config = gate_cfg
    gate.set_enabled(True)
    if not gate._model_ready():
        raise RuntimeError("ML model not trained — run --phase train first")
    kept: list[dict] = []
    blocked: list[dict] = []
    for rec in records:
        sc = _scenario_dict(rec)
        ml = gate.predict_for_trade(
            sc,
            rec["pair"],
            rec["trade"],
            rec.get("inst_config") or {},
            rec.get("armed_at_ms"),
        )
        row = {**rec, "ml": ml}
        if gate.should_block(ml, scenario=sc):
            blocked.append(row)
        else:
            kept.append(row)
    return kept, blocked


def _compare_mode_on_records(
    records: list[dict],
    *,
    model_mode: str | None,
    gate_cfg: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    kept, blocked = _gate_filter_records(records, model_mode=model_mode, gate_cfg=gate_cfg)
    port = _aggregate_mode(kept)
    port["blocked"] = len(blocked)
    port["blocked_pnl_usdt"] = round(sum(float(r.get("profit_abs") or 0) for r in blocked), 4)
    by_sid: dict[str, list[dict]] = defaultdict(list)
    for r in kept:
        by_sid[r["scenario_id"]].append(r)
    by_strategy = {sid: _aggregate_mode(rows) for sid, rows in sorted(by_sid.items())}
    return port, by_strategy


def phase_compare(args) -> dict[str, Any]:
    trades = _load_exported_trades()
    if not trades:
        raise RuntimeError(f"no exported trades in {EXPORT_DIR} — run --phase export first")

    threshold = float(getattr(args, "threshold", 0.0) or 0.0)
    gate_cfg = json.loads((ROOT / "simulation/config/ml_entry_gate.json").read_text(encoding="utf-8"))
    if threshold > 0:
        for key in ("grid_bots", "strategy_bots", "finder_bots"):
            if key in gate_cfg and isinstance(gate_cfg[key], dict):
                gate_cfg[key] = {**gate_cfg[key], "gate_mode": "profit_only", "min_confidence": threshold}
        gate_note = f"profit_only >= {int(threshold * 100)}%"
    else:
        gate_note = "from ml_entry_gate.json"

    print(f"=== COMPARE · gate {gate_note} ===")
    print(f"  OOS test split (newest 20% by open_ms) — честные цифры, модель не обучалась на этих сделках")
    oos_trades = _oos_records(trades)
    print(f"  signals: {len(trades)} total · {len(oos_trades)} OOS test")
    modes = {
        "no_ml": None,
        "global_ml": "global",
        "per_strategy_ml": "per_strategy",
    }

    results: dict[str, Any] = {
        "compared_at": datetime.now(tz=UTC).isoformat(),
        "n_signals": len(trades),
        "n_signals_oos": len(oos_trades),
        "methodology": (
            "Export runs with ML gate OFF (all backtest signals). "
            "Compare applies gate retrospectively on OOS test split only (newest 20%, "
            "same cutoff as train/test). portfolio_all_data is IN-SAMPLE and biased — do not use for decisions."
        ),
        "gate": gate_cfg,
        "train_meta": json.loads((MODEL_DIR / "pnl_classifier_meta.json").read_text(encoding="utf-8"))
        if (MODEL_DIR / "pnl_classifier_meta.json").is_file()
        else {},
        "portfolio": {},
        "portfolio_all_data": {},
        "by_strategy": {},
        "by_strategy_all_data": {},
    }

    compare_modes = getattr(args, "compare_modes", None) or ("no_ml", "global_ml", "per_strategy_ml")

    for mode_name, model_mode in modes.items():
        if mode_name not in compare_modes:
            continue
        port_oos, by_oos = _compare_mode_on_records(oos_trades, model_mode=model_mode, gate_cfg=gate_cfg)
        results["portfolio"][mode_name] = port_oos
        results["by_strategy"][mode_name] = by_oos

        port_all, by_all = _compare_mode_on_records(trades, model_mode=model_mode, gate_cfg=gate_cfg)
        results["portfolio_all_data"][mode_name] = port_all
        results["by_strategy_all_data"][mode_name] = by_all

        print(
            f"  {mode_name} OOS: {port_oos['trades']} trades · {port_oos['pnl_usdt']:+.2f} USDT · "
            f"WR {port_oos['win_rate']*100:.1f}% · blocked {port_oos.get('blocked', 0)}"
        )
        print(
            f"           (all-data biased: {port_all['trades']} tr · {port_all['pnl_usdt']:+.2f} USDT · "
            f"WR {port_all['win_rate']*100:.1f}%)"
        )

    # Confidence distribution on OOS only
    gate = MlEntryGate(ROOT, model_mode="global")
    confs: list[float] = []
    high80 = 0
    for rec in oos_trades:
        sc = _scenario_dict(rec)
        ml = gate.predict_for_trade(
            sc, rec["pair"], rec["trade"], rec.get("inst_config") or {}, rec.get("armed_at_ms")
        )
        cp = float(ml.get("confidence_profit") or 0)
        confs.append(cp)
        if cp >= 0.8:
            high80 += 1
    results["confidence"] = {
        "avg_profit_pct": round(100 * sum(confs) / len(confs), 2) if confs else 0,
        "pct_ge_80": round(100 * high80 / len(confs), 2) if confs else 0,
        "count_ge_80": high80,
        "thresholds": {
            f">={p}%": {
                "count": sum(1 for c in confs if c >= p / 100),
                "pct": round(100 * sum(1 for c in confs if c >= p / 100) / len(confs), 2),
            }
            for p in (50, 60, 70, 80, 90)
        },
    }

    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(getattr(args, "compare_output", "") or COMPARE_PATH)
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nComparison: {out_path}")
    return results


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Full ML study: export, train, compare")
    ap.add_argument(
        "--phase",
        choices=("export", "train", "compare", "all"),
        default="all",
    )
    ap.add_argument("--pairs", choices=("pool", "all", "prod200"), default="all")
    ap.add_argument("--timerange", default=DEFAULT_TIMERANGE)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument(
        "--scan-workers",
        type=int,
        default=0,
        help="Parallel workers for scanner replay (0=auto cpu_count, max 16)",
    )
    ap.add_argument("--model", default="lightgbm")
    ap.add_argument("--fresh", action="store_true", help="Clear export dir before run")
    ap.add_argument("--rerun-month", action="store_true")
    ap.add_argument("--bootstrap-db", action="store_true", help="Seed export from trade_db before train/compare")
    ap.add_argument("--skip-export", action="store_true", help="Skip sim export (use existing jsonl / bootstrap)")
    ap.add_argument(
        "--train-source",
        choices=("trade_db", "export", "all"),
        default="trade_db",
        help="Global model training data: trade_db, export jsonl only, or merged all",
    )
    ap.add_argument(
        "--threshold",
        type=float,
        default=0.0,
        help="Override gate min_confidence for compare (e.g. 0.6); 0 = use ml_entry_gate.json",
    )
    ap.add_argument(
        "--compare-output",
        default="",
        help="Write comparison JSON to this path (default: comparison.json)",
    )
    ap.add_argument(
        "--compare-modes",
        default="no_ml,global_ml,per_strategy_ml",
        help="Comma-separated: no_ml, global_ml, per_strategy_ml",
    )
    ap.add_argument("--skip-per-strategy", action="store_true", help="Skip per-scenario model training")
    ap.add_argument("--min-trades-per-strategy", type=int, default=MIN_TRADES)
    args = ap.parse_args()
    args.compare_modes = tuple(m.strip() for m in args.compare_modes.split(",") if m.strip())

    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    prod = json.loads(PROD_CFG.read_text(encoding="utf-8"))
    enabled = set(prod["enabled_scenarios"])

    if args.bootstrap_db:
        export_from_trade_db(enabled)

    if args.phase in ("export", "all") and not args.skip_export:
        phase_export(args)
    elif args.phase in ("train", "compare", "all") and not _load_exported_trades() and not args.bootstrap_db:
        print("WARN: no export data — bootstrapping from trade_db")
        export_from_trade_db(enabled)

    if args.phase in ("train", "all"):
        phase_train(args)
    if args.phase in ("compare", "all"):
        phase_compare(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

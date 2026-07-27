"""Persist scanner-gated sim trades into profit/loss folders with deduplication."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DB_DIR = "simulation/results/trade_db"
PROFIT_DIR = "profit"
LOSS_DIR = "loss"
RUNS_DIR = "runs"
INDEX_FILE = "index.json"


def safe_filename(key: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", key)


def trade_key(scenario_id: str, pair: str, trade: dict[str, Any]) -> str:
    return f"{scenario_id}|{pair}|{trade['open_ms']}|{trade['close_ms']}"


def _load_index(db_root: Path) -> dict[str, Any]:
    path = db_root / INDEX_FILE
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"trades": {}, "runs": []}


def _save_index(db_root: Path, index: dict[str, Any]) -> None:
    db_root.mkdir(parents=True, exist_ok=True)
    (db_root / INDEX_FILE).write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def _basis_for_instance(inst: dict[str, Any], trade: dict[str, Any], sc: dict[str, Any] | None) -> dict[str, Any]:
    cfg = inst.get("config") or {}
    return {
        "mode": "sim_scanner_gated",
        "scenario_id": inst.get("scenario_id"),
        "label": inst.get("label"),
        "strategy": inst.get("strategy"),
        "settings": inst.get("settings"),
        "group": inst.get("group"),
        "scan_type": (sc or {}).get("scan_type"),
        "pair": inst.get("pair"),
        "stake_usdt": cfg.get("stake"),
        "timeframe": cfg.get("timeframe"),
        "stoploss": cfg.get("stoploss"),
        "minimal_roi": cfg.get("minimal_roi"),
        "trading_active": inst.get("trading_active"),
        "armed_at_ms": inst.get("armed_at_ms"),
        "scan_armed_at_ms": inst.get("scan_armed_at_ms"),
        "exit_reason": trade.get("exit_reason"),
        "exit_basis": _exit_basis(trade.get("exit_reason")),
    }


def _exit_basis(exit_reason: str | None) -> str:
    if not exit_reason:
        return "unknown"
    r = exit_reason.lower()
    if "roi" in r:
        return "take_profit_roi"
    if "stop" in r:
        return "stop_loss"
    if "signal" in r or "exit" in r:
        return "strategy_exit_signal"
    if "force" in r:
        return "forced_exit"
    return exit_reason


def _build_record(
    inst: dict[str, Any],
    trade: dict[str, Any],
    sc: dict[str, Any] | None,
    *,
    run_id: str,
    range_ms: list[int],
) -> dict[str, Any]:
    key = trade_key(inst["scenario_id"], inst["pair"], trade)
    return {
        "id": key,
        "run_id": run_id,
        "scenario_id": inst["scenario_id"],
        "label": inst.get("label"),
        "pair": inst.get("pair"),
        "profit_abs": float(trade.get("profit_abs") or 0),
        "profit_ratio": float(trade.get("profit_ratio") or 0),
        "basis": _basis_for_instance(inst, trade, sc),
        "trade": trade,
        "range_ms": range_ms,
        "saved_at": datetime.now(tz=UTC).isoformat(),
    }


def persist_session(
    session: Any,
    root: Path,
    *,
    source: str = "prgon",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Save gated trades from BotSessionManager into trade_db (dedup by trade key)."""
    db_root = root / DB_DIR
    profit_dir = db_root / PROFIT_DIR
    loss_dir = db_root / LOSS_DIR
    runs_dir = db_root / RUNS_DIR
    profit_dir.mkdir(parents=True, exist_ok=True)
    loss_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    range_ms = session.status.get("range_ms") or [0, 0]
    ts = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
    run_id = run_id or f"{ts}_{range_ms[0]}_{range_ms[1]}"
    scenarios_by_id = {s["id"]: s for s in session.scenarios}

    index = _load_index(db_root)
    trades_index: dict[str, Any] = index.setdefault("trades", {})

    saved = 0
    skipped = 0
    profit_n = 0
    loss_n = 0
    profit_usdt = 0.0
    loss_usdt = 0.0
    by_scenario: dict[str, dict[str, Any]] = {}

    for inst in session.instances:
        sid = inst["scenario_id"]
        sc = scenarios_by_id.get(sid)
        bucket = by_scenario.setdefault(
            sid,
            {
                "scenario_id": sid,
                "label": inst.get("label"),
                "strategy": inst.get("strategy"),
                "trades_saved": 0,
                "trades_skipped": 0,
                "profit_abs": 0.0,
            },
        )
        for trade in inst.get("trades") or []:
            key = trade_key(sid, inst["pair"], trade)
            if key in trades_index:
                skipped += 1
                bucket["trades_skipped"] += 1
                continue
            record = _build_record(inst, trade, sc, run_id=run_id, range_ms=range_ms)
            pnl = float(trade.get("profit_abs") or 0)
            if pnl >= 0:
                dest = profit_dir / f"{safe_filename(key)}.json"
                profit_n += 1
                profit_usdt += pnl
            else:
                dest = loss_dir / f"{safe_filename(key)}.json"
                loss_n += 1
                loss_usdt += pnl
            dest.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            trades_index[key] = {
                "path": str(dest.relative_to(db_root)).replace("\\", "/"),
                "run_id": run_id,
                "scenario_id": sid,
                "pair": inst["pair"],
                "profit_abs": pnl,
                "saved_at": record["saved_at"],
            }
            saved += 1
            bucket["trades_saved"] += 1
            bucket["profit_abs"] = round(bucket["profit_abs"] + pnl, 6)

    run_manifest = {
        "run_id": run_id,
        "source": source,
        "range_ms": range_ms,
        "pool": session.status.get("sim_pool") or session.status.get("pool"),
        "scenarios_enabled": session.enabled_scenario_ids(),
        "instances": len(session.instances),
        "trades_saved": saved,
        "trades_skipped_duplicate": skipped,
        "profit_trades": profit_n,
        "loss_trades": loss_n,
        "profit_usdt": round(profit_usdt, 6),
        "loss_usdt": round(loss_usdt, 6),
        "net_usdt": round(profit_usdt + loss_usdt, 6),
        "by_scenario": list(by_scenario.values()),
        "finished_at": datetime.now(tz=UTC).isoformat(),
    }
    (runs_dir / f"{run_id}.json").write_text(
        json.dumps(run_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    index["runs"] = ([run_manifest] + [r for r in index.get("runs", []) if r.get("run_id") != run_id])[:50]
    _save_index(db_root, index)

    report = build_period_report(db_root)
    (db_root / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    return {**run_manifest, "report_path": str((db_root / "report.json").relative_to(root))}


def build_period_report(db_root: Path) -> dict[str, Any]:
    """Aggregate all trades in profit/loss folders."""
    index = _load_index(db_root)
    profit_dir = db_root / PROFIT_DIR
    loss_dir = db_root / LOSS_DIR
    profit_trades: list[dict] = []
    loss_trades: list[dict] = []
    for d, out in ((profit_dir, profit_trades), (loss_dir, loss_trades)):
        if not d.is_dir():
            continue
        for fp in d.glob("*.json"):
            try:
                out.append(json.loads(fp.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
    profit_usdt = sum(float(t.get("profit_abs") or 0) for t in profit_trades)
    loss_usdt = sum(float(t.get("profit_abs") or 0) for t in loss_trades)
    by_strategy: dict[str, dict[str, Any]] = {}
    for t in profit_trades + loss_trades:
        sid = t.get("scenario_id") or "?"
        row = by_strategy.setdefault(
            sid,
            {
                "scenario_id": sid,
                "label": t.get("label"),
                "trades": 0,
                "profit_abs": 0.0,
                "wins": 0,
                "losses": 0,
            },
        )
        row["trades"] += 1
        pnl = float(t.get("profit_abs") or 0)
        row["profit_abs"] = round(row["profit_abs"] + pnl, 6)
        if pnl >= 0:
            row["wins"] += 1
        else:
            row["losses"] += 1
    runs = index.get("runs") or []
    range_ms_all: list[list[int]] = [r["range_ms"] for r in runs if r.get("range_ms")]
    period_ms: list[int] | None = None
    if range_ms_all:
        period_ms = [min(r[0] for r in range_ms_all), max(r[1] for r in range_ms_all)]
    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "period_ms": period_ms,
        "total_unique_trades": len(index.get("trades") or {}),
        "profit_trades": len(profit_trades),
        "loss_trades": len(loss_trades),
        "profit_usdt": round(profit_usdt, 6),
        "loss_usdt": round(loss_usdt, 6),
        "net_usdt": round(profit_usdt + loss_usdt, 6),
        "by_strategy": sorted(by_strategy.values(), key=lambda x: x["profit_abs"], reverse=True),
        "runs_count": len(runs),
        "runs": runs[:10],
    }

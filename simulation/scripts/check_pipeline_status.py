#!/usr/bin/env python3
"""Check prod-year ML pipeline progress; resume if stalled."""
from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "simulation/results/full_ml_study/pipeline_run.log"
STATE = ROOT / "simulation/results/full_ml_study/export_state.json"
REPORT = ROOT / "simulation/results/full_ml_study/full_ml_study_report.txt"
COMPARE = ROOT / "simulation/results/full_ml_study/comparison.json"


def tail_text(path: Path, n: int = 30) -> str:
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def parse_phase(log: str) -> dict:
    out = {"phase": "unknown", "detail": ""}
    if "PIPELINE DONE" in log:
        out["phase"] = "done"
        return out
    if "Write TXT report" in log or "report_full_ml_study_txt" in log:
        out["phase"] = "report"
    elif "Compare no ML" in log or "=== COMPARE" in log:
        out["phase"] = "compare"
    elif "TRAIN global" in log or "Train Grid" in log:
        out["phase"] = "train"
    elif re.search(r"export \d{4}-\d{2}", log):
        m = re.findall(r"export (\d{4}-\d{2})", log)
        out["phase"] = "export"
        out["detail"] = m[-1] if m else ""
    elif re.search(r"batch \d+/\d+", log):
        m = re.findall(r"batch (\d+/\d+)", log)
        out["phase"] = "download"
        out["detail"] = m[-1] if m else ""
    elif "=== Download" in log or "download 185 pairs" in log:
        out["phase"] = "download"
    elif "Fetch prod" in log:
        out["phase"] = "fetch_pairs"
    return out


def load_export_state() -> dict:
    if STATE.is_file():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def main() -> int:
    log = LOG.read_text(encoding="utf-8", errors="replace") if LOG.is_file() else ""
    phase = parse_phase(log)
    export = load_export_state()
    pairs_have = 0
    prod_n = 0
    prod_path = ROOT / "simulation/config/prod_pairs_200.json"
    if prod_path.is_file():
        prod = json.loads(prod_path.read_text(encoding="utf-8")).get("pairs") or []
        prod_n = len(prod)
        try:
            from simulation.exchange_sim.datastore import HistoricalDatastore

            ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
            have = set(ds.list_pairs("5m"))
            pairs_have = sum(1 for p in prod if p in have)
        except Exception:
            pass

    status = {
        "checked_at": datetime.now(tz=UTC).isoformat(),
        "phase": phase["phase"],
        "phase_detail": phase.get("detail", ""),
        "log_bytes": LOG.stat().st_size if LOG.is_file() else 0,
        "export_trades": export.get("total_trades"),
        "export_months_done": len(export.get("completed_months") or []),
        "prod_pairs": prod_n,
        "pairs_with_ohlcv": pairs_have,
        "report_exists": REPORT.is_file(),
        "compare_exists": COMPARE.is_file(),
        "done": phase["phase"] == "done",
    }
    out_path = ROOT / "simulation/results/full_ml_study/pipeline_status.json"
    out_path.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(status, indent=2, ensure_ascii=False))
    if status["done"]:
        print("\nSTATUS: COMPLETE")
        return 0
    print(f"\nSTATUS: RUNNING ({status['phase']} {status.get('phase_detail', '')})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

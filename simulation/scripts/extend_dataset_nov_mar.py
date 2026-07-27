#!/usr/bin/env python3
"""Nov 2025 – Mar 2026: download 5m, prgon, fill trade_db, retrain classifier."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TIMERANGE = "20251101-20260331"


def main() -> int:
    py = str(ROOT / ".venv/Scripts/python.exe")
    fill = str(ROOT / "simulation/scripts/fill_trade_db_period.py")
    r = subprocess.run(
        [
            py,
            fill,
            "--timerange",
            TIMERANGE,
            "--skip-1s",
            "--train",
            "--workers",
            "3",
        ],
        cwd=str(ROOT),
    )
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())

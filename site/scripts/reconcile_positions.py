"""Compatibility entry. Implementation: trading/reconcile_positions.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "trading" / "reconcile_positions.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("reconcile_positions moved to trading.reconcile_positions. Import trading.reconcile_positions.")

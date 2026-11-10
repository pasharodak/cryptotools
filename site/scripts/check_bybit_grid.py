"""Compatibility entry. Implementation: grid/check_bybit_grid.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "grid" / "check_bybit_grid.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("check_bybit_grid moved to grid.check_bybit_grid. Import grid.check_bybit_grid.")

"""Compatibility entry. Implementation: grid/bybit_grid_manager.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "grid" / "bybit_grid_manager.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("bybit_grid_manager moved to grid.bybit_grid_manager. Import grid.bybit_grid_manager.")

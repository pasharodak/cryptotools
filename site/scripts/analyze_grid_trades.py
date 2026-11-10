"""Compatibility entry. Implementation: grid/analyze_grid_trades.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "grid" / "analyze_grid_trades.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("analyze_grid_trades moved to grid.analyze_grid_trades. Import grid.analyze_grid_trades.")

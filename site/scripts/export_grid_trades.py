"""Compatibility entry. Implementation: grid/export_grid_trades.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "grid" / "export_grid_trades.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("export_grid_trades moved to grid.export_grid_trades. Import grid.export_grid_trades.")

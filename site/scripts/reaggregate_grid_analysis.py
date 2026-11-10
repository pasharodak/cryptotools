"""Compatibility entry. Implementation: grid/reaggregate_grid_analysis.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "grid" / "reaggregate_grid_analysis.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("reaggregate_grid_analysis moved to grid.reaggregate_grid_analysis. Import grid.reaggregate_grid_analysis.")

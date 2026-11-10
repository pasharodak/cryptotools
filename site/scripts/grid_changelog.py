"""Compatibility entry. Implementation: grid/grid_changelog.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "grid" / "grid_changelog.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("grid_changelog moved to grid.grid_changelog. Import grid.grid_changelog.")

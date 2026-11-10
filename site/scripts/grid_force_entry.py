"""Compatibility entry. Implementation: grid/grid_force_entry.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "grid" / "grid_force_entry.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("grid_force_entry moved to grid.grid_force_entry. Import grid.grid_force_entry.")

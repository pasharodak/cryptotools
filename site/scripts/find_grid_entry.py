"""Compatibility entry. Implementation: grid/find_grid_entry.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "grid" / "find_grid_entry.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("find_grid_entry moved to grid.find_grid_entry. Import grid.find_grid_entry.")

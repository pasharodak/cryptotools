"""Compatibility entry. Implementation: grid/patch_grid_blacklist.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "grid" / "patch_grid_blacklist.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("patch_grid_blacklist moved to grid.patch_grid_blacklist. Import grid.patch_grid_blacklist.")

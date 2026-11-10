"""Compatibility entry. Implementation: trading/entry_constraints.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "trading" / "entry_constraints.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("entry_constraints moved to trading.entry_constraints. Import trading.entry_constraints.")

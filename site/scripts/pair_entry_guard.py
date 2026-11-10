"""Compatibility entry. Implementation: trading/pair_entry_guard.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "trading" / "pair_entry_guard.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("pair_entry_guard moved to trading.pair_entry_guard. Import trading.pair_entry_guard.")

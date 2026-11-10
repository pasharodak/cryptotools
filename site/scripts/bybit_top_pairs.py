"""Compatibility entry. Implementation: scans/bybit_top_pairs.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "scans" / "bybit_top_pairs.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("bybit_top_pairs moved to scans.bybit_top_pairs. Import scans.bybit_top_pairs.")

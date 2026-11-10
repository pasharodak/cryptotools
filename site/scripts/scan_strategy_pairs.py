"""Compatibility entry. Implementation: scans/scan_strategy_pairs.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "scans" / "scan_strategy_pairs.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("scan_strategy_pairs moved to scans.scan_strategy_pairs. Import scans.scan_strategy_pairs.")

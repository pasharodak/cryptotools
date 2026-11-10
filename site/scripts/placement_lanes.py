"""Compatibility entry. Implementation: trading/placement_lanes.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "trading" / "placement_lanes.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("placement_lanes moved to trading.placement_lanes. Import trading.placement_lanes.")

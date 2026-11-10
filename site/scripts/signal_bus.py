"""Compatibility entry. Implementation: trading/signal_bus.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "trading" / "signal_bus.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("signal_bus moved to trading.signal_bus. Import trading.signal_bus.")

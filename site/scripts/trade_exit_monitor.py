"""Compatibility entry. Implementation: trading/trade_exit_monitor.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "trading" / "trade_exit_monitor.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("trade_exit_monitor moved to trading.trade_exit_monitor. Import trading.trade_exit_monitor.")

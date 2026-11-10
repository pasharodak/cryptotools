"""Compatibility entry. Implementation: trading/trade_executor.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "trading" / "trade_executor.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("trade_executor moved to trading.trade_executor. Import trading.trade_executor.")

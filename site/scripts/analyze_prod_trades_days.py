"""Compatibility entry. Implementation: tools/analyze_prod_trades_days.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "tools" / "analyze_prod_trades_days.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("analyze_prod_trades_days moved to tools.analyze_prod_trades_days. Import tools.analyze_prod_trades_days.")

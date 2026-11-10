"""Compatibility entry. Implementation: trading/entry_policy.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "trading" / "entry_policy.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("entry_policy moved to trading.entry_policy. Import trading.entry_policy.")

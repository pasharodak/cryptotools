"""Compatibility entry. Implementation: retired/vibe_trade_signals.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "retired" / "vibe_trade_signals.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("vibe_trade_signals moved to retired.vibe_trade_signals. Import retired.vibe_trade_signals.")

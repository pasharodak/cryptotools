"""Compatibility entry. Implementation: tools/patch_stoploss.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "tools" / "patch_stoploss.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("patch_stoploss moved to tools.patch_stoploss. Import tools.patch_stoploss.")

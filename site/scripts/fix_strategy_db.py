"""Compatibility entry. Implementation: tools/fix_strategy_db.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "tools" / "fix_strategy_db.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("fix_strategy_db moved to tools.fix_strategy_db. Import tools.fix_strategy_db.")

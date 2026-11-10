"""Compatibility entry. Implementation: tools/merge_enabled_strategies_from_pack.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "tools" / "merge_enabled_strategies_from_pack.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("merge_enabled_strategies_from_pack moved to tools.merge_enabled_strategies_from_pack. Import tools.merge_enabled_strategies_from_pack.")

"""Compatibility entry. Implementation: tools/stack_watch_check.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "tools" / "stack_watch_check.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("stack_watch_check moved to tools.stack_watch_check. Import tools.stack_watch_check.")

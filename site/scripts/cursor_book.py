"""Compatibility entry. Implementation: retired/cursor_book.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "retired" / "cursor_book.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("cursor_book moved to retired.cursor_book. Import retired.cursor_book.")

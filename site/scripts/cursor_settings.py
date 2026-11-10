"""Compatibility entry. Implementation: retired/cursor_settings.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "retired" / "cursor_settings.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("cursor_settings moved to retired.cursor_settings. Import retired.cursor_settings.")

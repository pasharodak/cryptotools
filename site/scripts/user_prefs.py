"""Compatibility entry. Implementation: control/user_prefs.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "control" / "user_prefs.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("user_prefs moved to control.user_prefs. Import control.user_prefs.")

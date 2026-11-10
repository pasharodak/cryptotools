"""Compatibility entry. Implementation: scans/apply_pairlist_mode.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "scans" / "apply_pairlist_mode.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("apply_pairlist_mode moved to scans.apply_pairlist_mode. Import scans.apply_pairlist_mode.")

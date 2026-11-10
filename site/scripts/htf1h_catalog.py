"""Compatibility entry. Implementation: control/htf1h_catalog.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "control" / "htf1h_catalog.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("htf1h_catalog moved to control.htf1h_catalog. Import control.htf1h_catalog.")

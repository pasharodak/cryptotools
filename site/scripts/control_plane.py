"""Compatibility entry. Implementation: control/control_plane.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "control" / "control_plane.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("control_plane moved to control.control_plane. Import control.control_plane.")

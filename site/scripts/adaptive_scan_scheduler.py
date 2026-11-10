"""Compatibility entry. Implementation: scans/adaptive_scan_scheduler.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "scans" / "adaptive_scan_scheduler.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("adaptive_scan_scheduler moved to scans.adaptive_scan_scheduler. Import scans.adaptive_scan_scheduler.")

"""Compatibility entry. Implementation: tools/probe_live_entries.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "tools" / "probe_live_entries.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("probe_live_entries moved to tools.probe_live_entries. Import tools.probe_live_entries.")

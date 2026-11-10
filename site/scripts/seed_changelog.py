"""Compatibility entry. Implementation: tools/seed_changelog.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "tools" / "seed_changelog.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("seed_changelog moved to tools.seed_changelog. Import tools.seed_changelog.")

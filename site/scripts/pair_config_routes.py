"""Compatibility entry. Implementation: control/pair_config_routes.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "control" / "pair_config_routes.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("pair_config_routes moved to control.pair_config_routes. Import control.pair_config_routes.")

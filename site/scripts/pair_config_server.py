"""Compatibility entry. Implementation: control/pair_config_server.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "control" / "pair_config_server.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("pair_config_server moved to control.pair_config_server. Import control.pair_config_server.")

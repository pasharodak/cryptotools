"""Compatibility entry. Implementation: control/bot_orchestrator.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "control" / "bot_orchestrator.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("bot_orchestrator moved to control.bot_orchestrator. Import control.bot_orchestrator.")

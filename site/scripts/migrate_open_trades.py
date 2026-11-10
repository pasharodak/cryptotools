"""Compatibility entry. Implementation: tools/migrate_open_trades.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "tools" / "migrate_open_trades.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("migrate_open_trades moved to tools.migrate_open_trades. Import tools.migrate_open_trades.")

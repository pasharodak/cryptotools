"""Compatibility entry. Implementation: exchange/import_exchange_positions.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "exchange" / "import_exchange_positions.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("import_exchange_positions moved to exchange.import_exchange_positions. Import exchange.import_exchange_positions.")

"""Compatibility entry. Implementation: exchange/rest_client.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "exchange" / "rest_client.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("rest_client moved to exchange.rest_client. Import exchange.rest_client.")

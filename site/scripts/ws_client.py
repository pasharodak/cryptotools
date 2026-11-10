"""Compatibility entry. Implementation: exchange/ws_client.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "exchange" / "ws_client.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("ws_client moved to exchange.ws_client. Import exchange.ws_client.")

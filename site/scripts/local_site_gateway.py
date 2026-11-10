"""Compatibility entry. Implementation: tools/local_site_gateway.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "tools" / "local_site_gateway.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("local_site_gateway moved to tools.local_site_gateway. Import tools.local_site_gateway.")

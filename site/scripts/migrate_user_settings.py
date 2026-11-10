"""Compatibility entry. Implementation: tools/migrate_user_settings.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "tools" / "migrate_user_settings.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("migrate_user_settings moved to tools.migrate_user_settings. Import tools.migrate_user_settings.")

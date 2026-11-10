"""Compatibility entry. Implementation: tenants/tenant_manager.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "tenants" / "tenant_manager.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("tenant_manager moved to tenants.tenant_manager. Import tenants.tenant_manager.")

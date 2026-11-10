"""Compatibility entry. Implementation: tenants/tenant_context.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "tenants" / "tenant_context.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("tenant_context moved to tenants.tenant_context. Import tenants.tenant_context.")

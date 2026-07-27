#!/usr/bin/env python3
"""Apply prod ML bot pack locally."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    r = subprocess.run([sys.executable, str(ROOT / "simulation/scripts/apply_prod_ml_config.py")], cwd=str(ROOT))
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())

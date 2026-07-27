#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from grid_changelog import ensure_seeded

ensure_seeded()
print("changelog seeded")

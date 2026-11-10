#!/usr/bin/env python3
import sys
from pathlib import Path as _BootstrapPath

_scripts_root = _BootstrapPath(__file__).resolve().parents[1]
if str(_scripts_root) not in sys.path:
    sys.path.insert(0, str(_scripts_root))

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grid.bybit_grid_manager import get_bot_detail, get_status_payload, load_state

bid = sys.argv[1] if len(sys.argv) > 1 else "625311038199329439"
print("STATE:", json.dumps(load_state(), indent=2, ensure_ascii=False))
print("\nDETAIL:")
try:
    print(json.dumps(get_bot_detail(bid), indent=2, ensure_ascii=False))
except Exception as exc:
    print("ERROR:", exc)
print("\nSTATUS:")
print(json.dumps(get_status_payload(), indent=2, ensure_ascii=False))

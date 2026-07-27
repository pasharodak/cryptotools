#!/usr/bin/env python3
"""Deploy N Bybit grid bots; print status."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bybit_grid_manager import get_status_payload, load_config, save_config, update_config
from scan_bybit_grid import deploy_best


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    status = get_status_payload()
    active = [b for b in status.get("bots", []) if b.get("is_active")]
    cfg = load_config()
    need = len(active) + count
    if int(cfg.get("max_active_bots", 1)) < need:
        update_config({"max_active_bots": need})
        print(f"max_active_bots -> {need}")

    print(f"Active before: {len(active)}")
    for b in active:
        print(f"  - {b.get('pair')} ({b.get('bot_id')})")

    created = []
    errors = []
    for i in range(count):
        print(f"\n--- deploy {i + 1}/{count} ---")
        attempt = 0
        while attempt < 5:
            attempt += 1
            try:
                r = deploy_best(verbose=True)
                if r.get("deployed"):
                    pair = r.get("params", {}).get("pair") or r.get("scan", {}).get("best", {}).get("pair")
                    created.append({"pair": pair, "bot_id": r.get("create", {}).get("bot_id")})
                    print(f"OK {pair}")
                    break
                err = str(r.get("error", ""))
                errors.append(r)
                print(json.dumps(r, ensure_ascii=False, indent=2))
                if "enough money" in err.lower() or "insufficient" in err.lower():
                    break
                if "precision" in err.lower() and attempt < 5:
                    print("retry next pair...")
                    time.sleep(1)
                    continue
                break
            except Exception as exc:  # noqa: BLE001
                errors.append({"error": str(exc)})
                print(f"FAIL {exc}")
                break
        else:
            break
        if errors and not created and i == 0:
            break
        if errors and errors[-1].get("error") and "enough money" in str(errors[-1].get("error", "")).lower():
            break
        time.sleep(1.5)

    print("\n=== SUMMARY ===")
    print(json.dumps({"created": created, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if len(created) == count else 1


if __name__ == "__main__":
    raise SystemExit(main())

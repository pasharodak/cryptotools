#!/usr/bin/env python3
"""Migrate per-user prefs JSON → control_plane.sqlite user_settings.

Idempotent: skips keys already present in DB. Resets broken placement
(main empty + large hidden pack) to all-in-test seed.

Usage (from app root):
  .venv/bin/python scripts/migrate_user_settings.py
  .venv/bin/python scripts/migrate_user_settings.py --reset-placement
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import control_plane as cp
import tenant_manager as tm
import user_prefs as up


KEYS = (
    (up.KEY_PLACEMENT, "strategy_ui_placement.json", None),
    (
        up.KEY_ENABLED,
        "enabled_strategies.json",
        {"enabled": {}, "inverted": {}, "trained_risk": {}, "ml_confidence": {}},
    ),
    (up.KEY_BOT_LIMITS, "bot_limits.json", {}),
    (up.KEY_TEST_SETTINGS, "test_strategy_settings.json", dict(up.DEFAULT_TEST_SETTINGS)),
    (up.KEY_MAX_PER, "max_open_trades_per_strategy.json", {"value": 0}),
    (up.KEY_DUAL_HEDGE, "dual_hedge.json", {"enabled": False}),
)


def _user_ids() -> list[str]:
    ids = ["admin"]
    try:
        for u in tm.load_users():
            uid = str(u.get("id") or "").strip()
            if uid and uid not in ids:
                ids.append(uid)
    except Exception as exc:
        print(f"warn: load_users failed: {exc}")
    # Also scan tenants dirs for orphans
    tenants_root = getattr(tm, "TENANTS_DIR", None)
    if tenants_root is None:
        tenants_root = Path(tm.BASE) / "user_data" / "tenants"
    tenants_root = Path(tenants_root)
    if tenants_root.is_dir():
        for p in tenants_root.iterdir():
            if p.is_dir():
                uid = p.name.strip()
                if uid and uid not in ids:
                    ids.append(uid)
    return ids


def migrate_user(user_id: str, *, reset_placement: bool) -> dict[str, str]:
    status: dict[str, str] = {}
    for key, json_name, default in KEYS:
        if key == up.KEY_PLACEMENT and reset_placement:
            up.save_placement(user_id, up.default_placement())
            status[key] = "reset_seed"
            continue
        if cp.has_user_setting(user_id, key):
            # Still run load_placement to repair legacy bulk-hidden
            if key == up.KEY_PLACEMENT:
                pl = up.load_placement(user_id)
                status[key] = (
                    f"exists main={len(pl.get('main') or [])} "
                    f"hidden={len(pl.get('hidden') or [])}"
                )
            else:
                status[key] = "exists"
            continue
        # First-time migrate from JSON (or seed default)
        if key == up.KEY_PLACEMENT:
            pl = up.load_placement(user_id)
            status[key] = (
                f"migrated main={len(pl.get('main') or [])} "
                f"hidden={len(pl.get('hidden') or [])}"
            )
        elif key == up.KEY_ENABLED:
            up.load_enabled_payload(user_id)
            status[key] = "migrated"
        elif key == up.KEY_BOT_LIMITS:
            up.load_bot_limits_payload(user_id)
            status[key] = "migrated"
        elif key == up.KEY_TEST_SETTINGS:
            up.load_test_settings(user_id)
            status[key] = "migrated"
        elif key == up.KEY_MAX_PER:
            up.load_max_per_strategy(user_id)
            status[key] = "migrated"
        elif key == up.KEY_DUAL_HEDGE:
            up.load_dual_hedge(user_id)
            status[key] = "migrated"
        else:
            up.get_or_migrate(
                user_id, key, json_name=json_name, default=default if default is not None else {}
            )
            status[key] = "migrated"
    return status


def sync_demo_live_blocks() -> int:
    """Ensure Live/Demo default allowed_blocks (no cross panel)."""
    changed = 0
    users = tm.load_users()
    for u in users:
        uid = str(u.get("id") or "")
        kind = tm.account_kind(u)
        if kind == "demo":
            want = list(tm.DEMO_DEFAULT_ALLOWED_BLOCKS)
        elif kind == "live" and not tm.is_admin(u):
            want = list(tm.LIVE_DEFAULT_ALLOWED_BLOCKS)
        else:
            continue
        cur = u.get("allowed_blocks")
        if cur != want:
            try:
                tm.set_user_permissions(uid, allowed_blocks=want)
                changed += 1
                print(f"  blocks {uid}: {kind} updated")
            except Exception as exc:
                print(f"  blocks {uid}: FAIL {exc}")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--reset-placement",
        action="store_true",
        help="Force all users to main=[], hidden=[] (all strategies in Тестовые)",
    )
    ap.add_argument(
        "--skip-blocks",
        action="store_true",
        help="Do not rewrite Live/Demo allowed_blocks",
    )
    args = ap.parse_args()

    cp.ensure_init()
    ids = _user_ids()
    print(f"users: {len(ids)} -> {ids}")
    for uid in ids:
        st = migrate_user(uid, reset_placement=bool(args.reset_placement))
        print(f"[{uid}] " + ", ".join(f"{k}={v}" for k, v in st.items()))

    if not args.skip_blocks:
        print("sync Live/Demo allowed_blocks...")
        n = sync_demo_live_blocks()
        print(f"blocks updated: {n}")

    print("OK migrate_user_settings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

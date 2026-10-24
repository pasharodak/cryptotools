#!/usr/bin/env python3
"""One-shot trade watch snapshot for overnight/day monitoring."""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import reconcile_positions as rp  # noqa: E402


def _recent_opens(hours: float = 0.6) -> list[dict]:
    base = ROOT
    cut = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    out: list[dict] = []
    for bot, meta in rp.BOTS.items():
        db = rp.db_path_for_config(base, meta["config"])
        if not db or not db.is_file() or db.stat().st_size <= 0:
            continue
        with sqlite3.connect(str(db)) as conn:
            rows = conn.execute(
                """
                SELECT id, pair, is_open, is_short, open_date, enter_tag, exit_reason
                FROM trades WHERE open_date >= ? ORDER BY open_date DESC LIMIT 30
                """,
                (cut,),
            ).fetchall()
        for r in rows:
            out.append(
                {
                    "bot": bot,
                    "id": r[0],
                    "pair": r[1],
                    "is_open": int(r[2] or 0),
                    "is_short": int(r[3] or 0),
                    "open_date": r[4],
                    "tag": r[5],
                    "exit_reason": r[6],
                }
            )
    return out


def main() -> int:
    base = ROOT
    env_path = base / ".env"
    rep = rp.reconcile(base, env_path)
    recent = _recent_opens(0.6)
    opened = [t for t in recent if t["is_open"] == 1]
    closed_new = [t for t in recent if t["is_open"] == 0]
    pairs = Counter(t["pair"] for t in opened)
    multi = {p: n for p, n in pairs.items() if n > 1}

    feed = base / "user_data" / "signals" / "feed.jsonl"
    feed_n = 0
    last_sigs: list[dict] = []
    if feed.is_file():
        lines = feed.read_text(encoding="utf-8").splitlines()
        feed_n = len(lines)
        for line in lines[-8:]:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            last_sigs.append(
                {
                    "ts": ev.get("ts"),
                    "strategy_id": ev.get("strategy_id"),
                    "pair": ev.get("pair"),
                    "side": ev.get("side"),
                }
            )

    snap = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ok": bool(rep.get("ok")),
        "ft_open": rep.get("ft_open_count"),
        "bybit": rep.get("bybit_position_count"),
        "ghosts": rep.get("ghost_count"),
        "dupes": rep.get("duplicate_count"),
        "duplicate_pairs": rep.get("duplicate_pairs") or [],
        "ghost_list": rep.get("ghosts") or [],
        "dupe_list": rep.get("duplicates") or [],
        "multi_pair_opens": multi,
        "opened_last_36m": opened,
        "closed_last_36m": closed_new[:15],
        "feed_lines": feed_n,
        "last_signals": last_sigs,
        "stall_hint": None,
    }
    if not opened and not last_sigs:
        snap["stall_hint"] = "no_signals_and_no_opens"
    elif last_sigs and not opened and not closed_new:
        snap["stall_hint"] = "signals_but_no_fills"
    elif multi or (rep.get("duplicate_count") or 0) > 0:
        snap["stall_hint"] = "duplicates"
    elif (rep.get("ghost_count") or 0) > 0:
        snap["stall_hint"] = "ghosts"

    out_dir = base / "user_data" / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "trade_watch_latest.json"
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

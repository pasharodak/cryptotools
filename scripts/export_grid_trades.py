#!/usr/bin/env python3
"""Export Grid bot trades + orders from SQLite to JSON."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def export(db_path: Path, out_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM trades ORDER BY open_date")
    trades = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM orders ORDER BY order_date")
    orders = [dict(r) for r in cur.fetchall()]

    orders_by_ft = {}
    for o in orders:
        tid = o.get("ft_trade_id")
        orders_by_ft.setdefault(tid, []).append(o)

    for t in trades:
        t["orders"] = orders_by_ft.get(t["id"], [])
        t["entry_count"] = sum(
            1 for o in t["orders"] if o.get("ft_is_entry") == 1 and o.get("status") == "closed"
        )

    conn.close()
    payload = {
        "trade_count": len(trades),
        "closed_count": sum(1 for t in trades if t.get("is_open") == 0),
        "trades": trades,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")
    return payload


if __name__ == "__main__":
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tradesv3-grid.sqlite")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("grid_trades_export.json")
    data = export(db, out)
    print(json.dumps({"trades": data["trade_count"], "closed": data["closed_count"], "out": str(out)}))

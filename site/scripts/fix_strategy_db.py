#!/usr/bin/env python3
"""Fix strategy bot DB duplicate order blocking startup."""
from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

DB = Path("/home/freqtrade/freqtrade/tradesv3-strategy.sqlite")
DUPLICATE_ORDER_ID = "1c20af49-b9db-435c-b6a7-0a1009843f1b"


def main() -> int:
    if not DB.is_file():
        print(f"DB not found: {DB}")
        return 1

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    dup = cur.execute(
        "SELECT id, ft_trade_id, ft_pair FROM orders WHERE order_id=?",
        (DUPLICATE_ORDER_ID,),
    ).fetchone()
    if not dup:
        print("No duplicate order found — DB may already be OK")
        return 0

    print("Existing order row:", dict(dup))

    # Trade 4 tries to re-insert the same exchange order on startup.
    trade4 = cur.execute("SELECT id, pair, is_open FROM trades WHERE id=4").fetchone()
    if trade4 and trade4["is_open"]:
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
        cur.execute(
            """
            UPDATE trades SET
                is_open=0,
                close_date=?,
                close_profit=0,
                close_profit_abs=0,
                exit_reason='manual_fix_duplicate_order'
            WHERE id=4
            """,
            (now,),
        )
        print("Closed stuck open trade #4 (DOGE) in DB")

    conn.commit()
    conn.close()
    print("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

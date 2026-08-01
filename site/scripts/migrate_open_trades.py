#!/usr/bin/env python3
"""Copy open trades (+ orders) from legacy DB into target bot DB."""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path


def table_cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def copy_rows(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    where: str,
    params: tuple = (),
) -> int:
    cols = table_cols(src, table)
    dst_cols = set(table_cols(dst, table))
    use_cols = [c for c in cols if c in dst_cols]
    if not use_cols:
        return 0
    placeholders = ",".join("?" for _ in use_cols)
    col_sql = ",".join(use_cols)
    rows = src.execute(
        f"SELECT {col_sql} FROM {table} WHERE {where}",
        params,
    ).fetchall()
    for row in rows:
        dst.execute(
            f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({placeholders})",
            row,
        )
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="/home/freqtrade/freqtrade")
    parser.add_argument("--source", default="tradesv3.sqlite")
    parser.add_argument("--target", default="tradesv3-finder.sqlite")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    base = Path(args.base)
    src_path = base / args.source
    dst_path = base / args.target

    if not src_path.exists():
        raise SystemExit(f"Source DB not found: {src_path}")

    src = sqlite3.connect(src_path)
    open_trades = src.execute(
        "SELECT id, pair, open_date, amount FROM trades WHERE is_open=1 ORDER BY id"
    ).fetchall()
    print(f"Open trades in {src_path.name}: {len(open_trades)}")
    for row in open_trades:
        print(" ", row)

    if not open_trades:
        return

    if args.dry_run:
        print("Dry run — no changes written.")
        return

    backup = dst_path.with_suffix(dst_path.suffix + ".bak")
    if dst_path.exists():
        shutil.copy2(dst_path, backup)
        print(f"Backup: {backup}")

    dst = sqlite3.connect(dst_path)
    try:
        trade_ids = [t[0] for t in open_trades]
        id_list = ",".join(str(i) for i in trade_ids)
        n_trades = copy_rows(src, dst, "trades", f"id IN ({id_list})")
        n_orders = copy_rows(src, dst, "orders", f"ft_trade_id IN ({id_list})")
        dst.commit()
        print(f"Copied trades={n_trades}, orders={n_orders} -> {dst_path.name}")
    finally:
        dst.close()
        src.close()


if __name__ == "__main__":
    main()

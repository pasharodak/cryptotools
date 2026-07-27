#!/usr/bin/env python3
"""Export live trades from bot SQLite DBs into replay scenarios."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_manifest() -> dict[str, Any]:
    p = _root() / "simulation" / "config" / "manifest.json"
    return json.loads(p.read_text(encoding="utf-8"))


def export_db(name: str, path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"bot": name, "missing": True, "trades": [], "pairs": [], "pnl_actual": 0.0}
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT id, pair, is_open, open_date, close_date, open_rate, close_rate,
               stake_amount, amount, is_short, enter_tag, exit_reason,
               close_profit, close_profit_abs
        FROM trades
        ORDER BY open_date
        """
    ).fetchall()
    con.close()

    trades = []
    pairs: set[str] = set()
    pnl = 0.0
    for r in rows:
        d = dict(r)
        pairs.add(d["pair"])
        if not d["is_open"] and d["close_profit_abs"] is not None:
            pnl += float(d["close_profit_abs"])
        trades.append(
            {
                "id": d["id"],
                "pair": d["pair"],
                "is_open": bool(d["is_open"]),
                "open_date": d["open_date"],
                "close_date": d["close_date"],
                "is_short": bool(d["is_short"]),
                "enter_tag": d["enter_tag"] or "",
                "exit_reason": d["exit_reason"] or "",
                "stake_amount": d["stake_amount"],
                "close_profit_abs": d["close_profit_abs"],
            }
        )

    by_day: dict[str, list[str]] = defaultdict(list)
    for t in trades:
        if t["open_date"]:
            day = str(t["open_date"])[:10]
            if t["pair"] not in by_day[day]:
                by_day[day].append(t["pair"])

    return {
        "bot": name,
        "db": str(path),
        "trade_count": len(trades),
        "closed_count": sum(1 for t in trades if not t["is_open"]),
        "pnl_actual": round(pnl, 4),
        "pairs": sorted(pairs),
        "pairs_by_day": dict(sorted(by_day.items())),
        "trades": trades,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export live trades for simulation replay")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    cfg = load_manifest()
    root = _root()
    out = args.out or (root / "simulation" / "data" / "live_trades_export.json")

    bots: dict[str, Any] = {}
    all_pairs: set[str] = set()
    total_pnl = 0.0
    for name, rel in cfg["live_dbs"].items():
        db_path = root / rel
        block = export_db(name, db_path)
        bots[name] = block
        all_pairs.update(block.get("pairs", []))
        total_pnl += float(block.get("pnl_actual", 0))

    payload = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "timerange": cfg.get("timerange"),
        "starting_balance_usdt": cfg.get("starting_balance_usdt", 100),
        "actual_total_pnl_usdt": round(total_pnl, 4),
        "all_pairs": sorted(all_pairs),
        "bots": bots,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Pairs: {len(all_pairs)} | Actual combined PnL: {total_pnl:.4f} USDT")
    for name, b in bots.items():
        if b.get("missing"):
            print(f"  {name}: DB missing ({b.get('db', '')})")
        else:
            print(f"  {name}: {b['closed_count']} closed, PnL {b['pnl_actual']} USDT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

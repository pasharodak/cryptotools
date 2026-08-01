#!/usr/bin/env python3
"""Analyze prod bot trades for given UTC calendar days."""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_DIR = ROOT / "simulation/data/live_dbs"
OUT_DIR = ROOT / "simulation/results"

BOTS = {
    "ML Finder": ("finder", "tradesv3-finder.sqlite"),
    "Strategy": ("strategy", "tradesv3-strategy.sqlite"),
    "Grid": ("grid", "tradesv3-grid.sqlite"),
}


def load_ml_meta(db_path: Path, trade_ids: list[int]) -> dict[int, dict]:
    if not trade_ids:
        return {}
    ph = ",".join("?" * len(trade_ids))
    q = f"""
        SELECT ft_trade_id, cd_key, cd_value, cd_type FROM trade_custom_data
        WHERE ft_trade_id IN ({ph})
          AND cd_key IN ('ml_confidence', 'ml_gate_confidence', 'ml_predicted')
    """
    out: dict[int, dict] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            for tid, key, val, typ in conn.execute(q, trade_ids):
                entry = out.setdefault(int(tid), {})
                if typ == "float":
                    entry[key] = float(val)
                elif typ == "int":
                    entry[key] = int(val)
                else:
                    entry[key] = val
    except sqlite3.Error:
        pass
    return out


def analyze_days(days: tuple[str, ...]) -> dict:
    all_trades: list[dict] = []
    summary: dict[str, dict] = {}

    for label, (_bot_key, dbn) in BOTS.items():
        path = DB_DIR / dbn
        if not path.is_file():
            continue
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT id, pair, enter_tag, strategy, is_open, open_date, close_date,
                   close_profit_abs, close_profit, exit_reason, stake_amount, is_short, leverage
            FROM trades
            WHERE date(close_date) IN ({})
               OR (is_open=1 AND date(open_date) IN ({}))
            ORDER BY COALESCE(close_date, open_date)
            """.format(",".join("?" * len(days)), ",".join("?" * len(days))),
            (*days, *days),
        ).fetchall()
        ml = load_ml_meta(path, [r["id"] for r in rows])
        bot_rows: list[dict] = []
        for r in rows:
            d = dict(r)
            d["bot"] = label
            d["ml"] = ml.get(r["id"], {})
            bot_rows.append(d)
            all_trades.append(d)

        closed = [t for t in bot_rows if not t["is_open"]]
        open_t = [t for t in bot_rows if t["is_open"]]
        pnl = sum(float(t["close_profit_abs"] or 0) for t in closed)
        wins = sum(1 for t in closed if float(t["close_profit"] or 0) > 0)
        by_day: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0, "l": 0})
        for t in closed:
            day = (t["close_date"] or "")[:10]
            by_day[day]["n"] += 1
            by_day[day]["pnl"] += float(t["close_profit_abs"] or 0)
            if float(t["close_profit"] or 0) > 0:
                by_day[day]["w"] += 1
            else:
                by_day[day]["l"] += 1

        by_pair: dict[str, float] = defaultdict(float)
        by_exit: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0.0})
        for t in closed:
            by_pair[t["pair"]] += float(t["close_profit_abs"] or 0)
            ex = t["exit_reason"] or "unknown"
            by_exit[ex]["n"] += 1
            by_exit[ex]["pnl"] += float(t["close_profit_abs"] or 0)

        summary[label] = {
            "closed": len(closed),
            "open": len(open_t),
            "pnl": round(pnl, 4),
            "wins": wins,
            "losses": len(closed) - wins,
            "by_day": {k: {**v, "pnl": round(v["pnl"], 4)} for k, v in sorted(by_day.items())},
            "by_pair": {k: round(v, 4) for k, v in sorted(by_pair.items(), key=lambda x: x[1])},
            "by_exit": {k: {"n": v["n"], "pnl": round(v["pnl"], 4)} for k, v in by_exit.items()},
            "trades": bot_rows,
        }
        con.close()

    return {"days": list(days), "summary": summary, "trades": all_trades}


def print_report(data: dict) -> None:
    days = data["days"]
    summary = data["summary"]
    print(f"=== PROD TRADES {days[0]} — {days[-1]} (UTC calendar days) ===\n")

    total_pnl = sum(s["pnl"] for s in summary.values())
    total_closed = sum(s["closed"] for s in summary.values())
    total_open = sum(s["open"] for s in summary.values())
    print(f"Portfolio: {total_closed} closed, {total_open} open | PnL {total_pnl:+.4f} USDT\n")

    for label, s in summary.items():
        wr = f"{100*s['wins']/s['closed']:.0f}%" if s["closed"] else "—"
        print(f"{label}: {s['closed']} closed, {s['open']} open | {s['pnl']:+.4f} USDT | W{s['wins']} L{s['losses']} WR {wr}")
        for day, d in s.get("by_day", {}).items():
            print(f"  {day}: {d['n']} tr, {d['pnl']:+.4f} USDT, W{d['w']} L{d['l']}")
        if s.get("by_pair"):
            worst = sorted(s["by_pair"].items(), key=lambda x: x[1])[:3]
            best = sorted(s["by_pair"].items(), key=lambda x: x[1], reverse=True)[:3]
            if worst:
                print(f"  worst pairs: {', '.join(f'{p} {v:+.2f}' for p,v in worst)}")
            if best:
                print(f"  best pairs:  {', '.join(f'{p} {v:+.2f}' for p,v in best)}")
        print()

    for label, s in summary.items():
        if not s["trades"]:
            continue
        print(f"--- {label} ---")
        for t in s["trades"]:
            st = "OPEN" if t["is_open"] else "CLOSED"
            ml = t.get("ml") or {}
            ml_parts = []
            if ml.get("ml_predicted"):
                ml_parts.append(str(ml["ml_predicted"]))
            conf = ml.get("ml_gate_confidence") or ml.get("ml_confidence")
            if conf is not None:
                ml_parts.append(f"{float(conf)*100:.0f}%")
            ml_s = f" ML[{' '.join(ml_parts)}]" if ml_parts else ""
            side = "SHORT" if t["is_short"] else "LONG"
            ts = (t["close_date"] or t["open_date"] or "")[:16]
            ex = t["exit_reason"] or "-"
            if t["is_open"]:
                pnl_s = "..."
            else:
                pnl_s = f"{float(t['close_profit_abs']):+.4f}"
            tag = t["enter_tag"] or "-"
            print(f"  {ts} {st:6} {t['pair']:18} [{tag:20}] {side:5} {pnl_s:>8} USDT | {ex}{ml_s}")
        print()


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", default=["2026-07-05", "2026-07-06"])
    args = ap.parse_args()
    days = tuple(args.days)

    data = analyze_days(days)
    # strip trades from summary for json compactness
    payload = {
        "days": data["days"],
        "summary": {k: {kk: vv for kk, vv in v.items() if kk != "trades"} for k, v in data["summary"].items()},
        "trades": data["trades"],
    }
    tag = f"{days[0]}_{days[-1]}".replace("-", "")
    out_json = OUT_DIR / f"prod_trades_analysis_{tag}.json"
    out_txt = OUT_DIR / f"prod_trades_analysis_{tag}.txt"
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        print_report(data)
    text = buf.getvalue()
    print(text)
    out_txt.write_text(text, encoding="utf-8")
    print(f"Saved: {out_json}")
    print(f"Saved: {out_txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

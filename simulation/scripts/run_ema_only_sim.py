#!/usr/bin/env python3
"""Export SimEmaGoldenCross (trend_ema) trades only — for manual review."""
from __future__ import annotations

import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.scripts.comparison_common import PROD_PAIRS_PATH, period_months  # noqa: E402
from simulation.scripts.run_full_ml_study import (  # noqa: E402
    _save_trade_files,
    _trade_outcome,
    run_export_month,
)

OUT = ROOT / "simulation/results/ema_only_study"
EXPORT = OUT / "export"
ENABLED = {"trend_ema"}
DEFAULT_RANGE = "20260401-20260706"


def _ms_to_iso(ms: int | None) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def write_report(records: list[dict]) -> None:
  rows = []
  for rec in sorted(records, key=lambda r: (r.get("trade") or {}).get("open_ms") or 0):
    tr = rec.get("trade") or {}
    pnl = float(rec.get("profit_abs") or 0)
    rows.append(
      {
        "pair": rec.get("pair", ""),
        "side": "short" if tr.get("is_short") else "long",
        "open": _ms_to_iso(tr.get("open_ms")),
        "close": _ms_to_iso(tr.get("close_ms")),
        "profit_usdt": round(pnl, 4),
        "profit_pct": round(float(tr.get("profit_ratio") or 0) * 100, 2),
        "exit_reason": tr.get("exit_reason") or "",
        "duration_min": tr.get("trade_duration") or "",
      }
    )

  OUT.mkdir(parents=True, exist_ok=True)
  csv_path = OUT / "ema_trades.csv"
  with csv_path.open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["pair"])
    w.writeheader()
    w.writerows(rows)

  wins = sum(1 for r in rows if r["profit_usdt"] >= 0)
  net = sum(r["profit_usdt"] for r in rows)
  lines = [
    "EMA 50/200 (4H) — SimEmaGoldenCross · trend_ema only",
    f"Trades: {len(rows)} · W{wins} L{len(rows)-wins} · WR {wins/len(rows)*100:.1f}%" if rows else "No trades",
    f"Net PnL: {net:+.2f} USDT" if rows else "",
    "",
    f"{'pair':<22} {'side':<6} {'open':<18} {'PnL':>8} {'exit':<20}",
    "-" * 80,
  ]
  for r in rows:
    lines.append(
      f"{r['pair']:<22} {r['side']:<6} {r['open']:<18} {r['profit_usdt']:+8.2f} {str(r['exit_reason'])[:20]}"
    )
  txt = OUT / "ema_trades_report.txt"
  txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
  print(f"Report: {txt}")
  print(f"CSV:    {csv_path}")


def main() -> int:
  import argparse

  ap = argparse.ArgumentParser()
  ap.add_argument("--timerange", default=DEFAULT_RANGE)
  ap.add_argument("--pairs", choices=("prod200", "pool"), default="prod200")
  ap.add_argument("--workers", type=int, default=1)
  args = ap.parse_args()

  if args.pairs == "prod200":
    pairs = list(json.loads((ROOT / PROD_PAIRS_PATH).read_text(encoding="utf-8")).get("pairs") or [])
    pool_mode = "direct"
  else:
    pairs = list(json.loads((ROOT / "simulation/config/player_pair_pool.json").read_text(encoding="utf-8")).get("pairs") or [])
    pool_mode = "profile"

  start_s, end_s = args.timerange.split("-")
  y0, m0 = int(start_s[:4]), int(start_s[4:6])
  y1, m1 = int(end_s[:4]), int(end_s[4:6])

  if EXPORT.is_dir():
    import shutil
    shutil.rmtree(EXPORT)
  OUT.mkdir(parents=True, exist_ok=True)

  all_records: list[dict] = []
  print(f"=== EMA ONLY · {len(pairs)} pairs · {args.timerange} · pool={pool_mode} ===")
  for y, m in period_months(y0, m0, y1, m1):
    label, records, _ = run_export_month(pairs, y, m, args.workers, ENABLED, pool_mode=pool_mode)
    _save_trade_files(EXPORT, records)
    w = sum(1 for r in records if _trade_outcome(r["trade"]) == "wins")
    print(f"  {label}: {len(records)} trades · W{w} L{len(records)-w}")
    all_records.extend(records)

  meta = {
    "scenario": "trend_ema",
    "strategy": "SimEmaGoldenCross",
    "timerange": args.timerange,
    "pairs": len(pairs),
    "trades": len(all_records),
  }
  (OUT / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
  write_report(all_records)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())

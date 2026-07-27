#!/usr/bin/env python3
"""Backtest ML trade finder scanner on selected pairs."""
from __future__ import annotations

import calendar
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.trade_finder import backtest_pairs, load_config, parse_timerange_ms  # noqa: E402

MONTHS = [
    (2025, 11),
    (2025, 12),
    (2026, 1),
    (2026, 2),
    (2026, 3),
    (2026, 4),
    (2026, 5),
    (2026, 6),
]


def load_pairs(root: Path, source: str) -> list[str]:
    if source == "extension":
        path = root / "simulation/config/extension_pairs.json"
    elif source == "pool":
        path = root / "simulation/config/player_pair_pool.json"
    else:
        raise ValueError(f"unknown pair source: {source}")
    return json.loads(path.read_text(encoding="utf-8"))["pairs"]


def month_range(year: int, month: int) -> tuple[int, int, str]:
    last = calendar.monthrange(year, month)[1]
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year, month, last, 23, 59, 59, tzinfo=UTC)
    label = f"{year}-{month:02d}"
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), label


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Backtest ML trade finder")
    ap.add_argument("--pairs", choices=["extension", "pool"], default="extension")
    ap.add_argument("--timerange", default="20251101-20260625")
    ap.add_argument("--monthly", action="store_true", help="Break down by month")
    ap.add_argument("--gate", action="store_true", help="Apply pnl_classifier gate after finder")
    ap.add_argument("--no-gate", action="store_true", help="Finder only (ignore config gate)")
    args = ap.parse_args()

    pairs = load_pairs(ROOT, args.pairs)
    cfg = load_config(ROOT)
    print(f"=== ML trade finder backtest ===")
    print(f"pairs ({args.pairs}): {len(pairs)}")
    print(f"min_confidence: {cfg['min_confidence']}  stake: {cfg['stake_usdt']} USDT")
    scan_stride = int(cfg.get("scan_stride") or cfg.get("sample_stride") or 12)
    tf = cfg.get("timeframe", "5m")
    bar_min = 5 if tf == "5m" else 1
    print(f"scan every {scan_stride * bar_min} min (stride={scan_stride} on {tf})")
    print(f"barrier: TP={cfg['tp_atr_mult']}xATR SL={cfg['sl_atr_mult']}xATR max_bars={cfg['max_bars']}")

    use_gate = None
    if args.gate:
        use_gate = True
    elif args.no_gate:
        use_gate = False

    if args.monthly:
        rows = []
        print(f"\n{'Month':<8} {'Signals':>8} {'PnL':>10} {'Win%':>6}")
        print("-" * 36)
        for y, m in MONTHS:
            s_ms, e_ms, label = month_range(y, m)
            tr_start, tr_end = parse_timerange_ms(args.timerange)
            start_ms = max(s_ms, tr_start)
            end_ms = min(e_ms, tr_end)
            if start_ms >= end_ms:
                continue
            print(f"  {label}...", flush=True)
            rep = backtest_pairs(ROOT, pairs, start_ms=start_ms, end_ms=end_ms, use_classifier_gate=use_gate)
            rows.append({
                "month": label,
                "signals": rep["total_signals"],
                "pnl_usdt": rep["total_pnl_usdt"],
                "win_rate": rep["win_rate"],
            })
            print(f"{label:<8} {rep['total_signals']:>8} {rep['total_pnl_usdt']:>+10.2f} {rep['win_rate']*100:>5.1f}%")
        total_pnl = sum(r["pnl_usdt"] for r in rows)
        total_sig = sum(r["signals"] for r in rows)
        print("-" * 36)
        print(f"{'TOTAL':<8} {total_sig:>8} {total_pnl:>+10.2f}")
        out = {"pairs_source": args.pairs, "pairs": pairs, "months": rows, "total_pnl_usdt": round(total_pnl, 4)}
    else:
        rep = backtest_pairs(ROOT, pairs, timerange=args.timerange, use_classifier_gate=use_gate)
        print(f"\nSignals: {rep['total_signals']}  PnL: {rep['total_pnl_usdt']:+.2f} USDT")
        print(f"Win rate: {rep['win_rate']*100:.1f}%  ({rep['wins']}W / {rep['losses']}L)")
        print("\nBy pair:")
        for pair, s in sorted(rep["by_pair"].items(), key=lambda x: -x[1]["pnl_usdt"]):
            print(f"  {pair:<22} {s['signals']:>4} sig  {s['pnl_usdt']:>+8.2f} USDT")
        out = {k: v for k, v in rep.items() if k != "signals"}
        out["pairs_source"] = args.pairs

    tag = "extension" if args.pairs == "extension" else "pool"
    out_path = ROOT / f"simulation/results/trade_db/models/trade_finder_backtest_{tag}.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

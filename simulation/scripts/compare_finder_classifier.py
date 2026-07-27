#!/usr/bin/env python3
"""Compare trade finder alone vs finder + pnl_classifier gate."""
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

MONTHS = [(2025, 11), (2025, 12), (2026, 1), (2026, 2), (2026, 3), (2026, 4), (2026, 5), (2026, 6)]


def load_pairs(root: Path, source: str) -> list[str]:
    if source == "extension":
        path = root / "simulation/config/extension_pairs.json"
    else:
        path = root / "simulation/config/player_pair_pool.json"
    return json.loads(path.read_text(encoding="utf-8"))["pairs"]


def month_range(year: int, month: int) -> tuple[int, int, str]:
    last = calendar.monthrange(year, month)[1]
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year, month, last, 23, 59, 59, tzinfo=UTC)
    label = f"{year}-{month:02d}"
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), label


def summarize(rep: dict) -> dict:
    return {
        "signals": rep["total_signals"],
        "skipped": rep.get("gate_skipped", 0),
        "skipped_pnl_usdt": rep.get("skipped_pnl_usdt", 0),
        "pnl_usdt": rep["total_pnl_usdt"],
        "win_rate": rep["win_rate"],
    }


def run_mode(pairs: list[str], use_gate: bool, timerange: str, monthly: bool) -> dict:
    label = "finder+classifier" if use_gate else "finder only"
    print(f"\n=== {label} ===")
    if monthly:
        rows = []
        tr_start, tr_end = parse_timerange_ms(timerange)
        for y, m in MONTHS:
            s_ms, e_ms, ml = month_range(y, m)
            start_ms = max(s_ms, tr_start)
            end_ms = min(e_ms, tr_end)
            if start_ms >= end_ms:
                continue
            print(f"  {ml}...", flush=True)
            rep = backtest_pairs(ROOT, pairs, start_ms=start_ms, end_ms=end_ms, use_classifier_gate=use_gate)
            rows.append({"month": ml, **summarize(rep)})
        total = {
            "signals": sum(r["signals"] for r in rows),
            "skipped": sum(r["skipped"] for r in rows),
            "skipped_pnl_usdt": round(sum(r["skipped_pnl_usdt"] for r in rows), 4),
            "pnl_usdt": round(sum(r["pnl_usdt"] for r in rows), 4),
        }
        return {"mode": label, "use_gate": use_gate, "months": rows, "total": total}
    rep = backtest_pairs(ROOT, pairs, timerange=timerange, use_classifier_gate=use_gate)
    return {"mode": label, "use_gate": use_gate, "total": summarize(rep), "by_pair": rep["by_pair"]}


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Finder vs finder+classifier")
    ap.add_argument("--pairs", choices=["extension", "pool"], default="extension")
    ap.add_argument("--timerange", default="20251101-20260625")
    ap.add_argument("--monthly", action="store_true")
    args = ap.parse_args()

    pairs = load_pairs(ROOT, args.pairs)
    cfg = load_config(ROOT)
    gate_cfg = cfg.get("classifier_gate") or {}
    scan_stride = int(cfg.get("scan_stride") or 4)
    print("=== Finder + Classifier comparison ===")
    print(f"pairs: {args.pairs} ({len(pairs)})  scan: every {scan_stride * 5} min")
    print(f"classifier scenario: {gate_cfg.get('scenario_id', 'trend_ema')}  mode: {gate_cfg.get('gate_mode', 'block_loss')}")

    finder = run_mode(pairs, False, args.timerange, args.monthly)
    combined = run_mode(pairs, True, args.timerange, args.monthly)

    f = finder["total"]
    c = combined["total"]
    delta = round(c["pnl_usdt"] - f["pnl_usdt"], 4)

    print("\n=== SUMMARY ===")
    print(f"{'Mode':<22} {'Signals':>8} {'Skip':>6} {'SkipPnL':>9} {'PnL':>10} {'Delta':>8}")
    print("-" * 68)
    print(f"{'Finder only':<22} {f['signals']:>8} {'':>6} {'':>9} {f['pnl_usdt']:>+10.2f} {'':>8}")
    print(
        f"{'Finder+classifier':<22} {c['signals']:>8} {c['skipped']:>6} "
        f"{c['skipped_pnl_usdt']:>+9.2f} {c['pnl_usdt']:>+10.2f} {delta:>+8.2f}"
    )

    if args.monthly and "months" in finder:
        print(f"\n{'Month':<8} {'F sig':>6} {'F PnL':>9} {'FC sig':>6} {'FC PnL':>9} {'Delta':>8}")
        print("-" * 50)
        fm = {r["month"]: r for r in finder["months"]}
        cm = {r["month"]: r for r in combined["months"]}
        for ml in sorted(fm):
            fr, cr = fm[ml], cm[ml]
            d = cr["pnl_usdt"] - fr["pnl_usdt"]
            print(
                f"{ml:<8} {fr['signals']:>6} {fr['pnl_usdt']:>+9.2f} "
                f"{cr['signals']:>6} {cr['pnl_usdt']:>+9.2f} {d:>+8.2f}"
            )

    out = {
        "compared_at": datetime.now(tz=UTC).isoformat(),
        "pairs_source": args.pairs,
        "timerange": args.timerange,
        "scan_minutes": scan_stride * 5,
        "classifier_gate": gate_cfg,
        "finder_only": finder,
        "finder_classifier": combined,
        "delta_pnl_usdt": delta,
    }
    tag = args.pairs
    out_path = ROOT / f"simulation/results/trade_db/models/finder_classifier_{tag}.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

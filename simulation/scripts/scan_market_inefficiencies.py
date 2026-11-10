#!/usr/bin/env python3
"""Scan crypto USDT markets for common inefficiencies (arbitrage-style).

Inspired by typical live-trading talks on inefficiencies / arb:
  1) Spot–perp basis on Bybit (cash-and-carry style dislocation)
  2) Extreme Bybit perpetual funding
  3) Cross-exchange perp mid spread Bybit ↔ Binance
  4) Funding divergence Bybit ↔ Binance

Public market data only (no keys). Not financial advice — spreads may be
untradeable after fees / depth / withdrawal delays.

Usage:
  python simulation/scripts/scan_market_inefficiencies.py
  python simulation/scripts/scan_market_inefficiencies.py --min-basis 0.15 --min-xex 0.25
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import ccxt  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise SystemExit("ccxt required: pip install ccxt") from exc


@dataclass
class Hit:
    kind: str
    symbol: str
    score: float
    detail: str
    metrics: dict[str, Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _mid(t: dict[str, Any] | None) -> float | None:
    if not t:
        return None
    bid = t.get("bid")
    ask = t.get("ask")
    last = t.get("last")
    if bid and ask and bid > 0 and ask > 0:
        return (float(bid) + float(ask)) / 2.0
    if last and float(last) > 0:
        return float(last)
    return None


def _pct(a: float, b: float) -> float:
    """(a - b) / b * 100."""
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


def _load_tickers(ex: Any, params: dict | None = None) -> dict[str, Any]:
    return ex.fetch_tickers(params=params or {})


def _bybit_funding_map(ex: Any) -> dict[str, float]:
    """symbol -> funding rate (fraction, e.g. 0.0001 = 0.01%)."""
    out: dict[str, float] = {}
    try:
        # ccxt unified: fetchFundingRates when available
        if hasattr(ex, "fetch_funding_rates"):
            rates = ex.fetch_funding_rates()
            for sym, row in (rates or {}).items():
                fr = row.get("fundingRate")
                if fr is not None:
                    out[sym] = float(fr)
            if out:
                return out
    except Exception:
        pass
    # Fallback: tickers often embed funding on bybit swap
    try:
        ticks = ex.fetch_tickers()
        for sym, t in ticks.items():
            info = t.get("info") or {}
            fr = info.get("fundingRate") or info.get("funding_rate")
            if fr is not None:
                out[sym] = float(fr)
    except Exception:
        pass
    return out


def _binance_funding_map(ex: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        if hasattr(ex, "fetch_funding_rates"):
            rates = ex.fetch_funding_rates()
            for sym, row in (rates or {}).items():
                fr = row.get("fundingRate")
                if fr is not None:
                    out[sym] = float(fr)
            if out:
                return out
    except Exception:
        pass
    return out


def scan(
    *,
    min_basis_pct: float,
    min_funding_pct: float,
    min_xex_pct: float,
    min_fund_div_pct: float,
    min_quote_vol: float,
    top_n: int,
) -> dict[str, Any]:
    bybit = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    bybit_spot = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    binance = ccxt.binanceusdm({"enableRateLimit": True})

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=5) as pool:
        f_swap = pool.submit(_load_tickers, bybit)
        f_spot = pool.submit(_load_tickers, bybit_spot)
        f_bn = pool.submit(_load_tickers, binance)
        f_bf = pool.submit(_bybit_funding_map, bybit)
        f_nf = pool.submit(_binance_funding_map, binance)
        bybit_swap = f_swap.result()
        bybit_spot_t = f_spot.result()
        binance_t = f_bn.result()
        bybit_fr = f_bf.result()
        binance_fr = f_nf.result()

    hits: list[Hit] = []

    # --- 1) Spot–perp basis on Bybit ---
    for sym, st in bybit_swap.items():
        if not sym.endswith("/USDT:USDT"):
            continue
        spot_sym = sym.replace(":USDT", "")
        spot = bybit_spot_t.get(spot_sym)
        mid_p = _mid(st)
        mid_s = _mid(spot)
        if mid_p is None or mid_s is None:
            continue
        qv = float((st.get("quoteVolume") or 0) or 0)
        if qv < min_quote_vol:
            continue
        basis = _pct(mid_p, mid_s)  # perp vs spot %
        if abs(basis) < min_basis_pct or abs(basis) > 15.0:
            continue
        ratio = max(mid_p, mid_s) / min(mid_p, mid_s)
        if ratio > 1.2:
            continue
        side = "perp>spot (short perp / long spot)" if basis > 0 else "perp<spot (long perp / short spot)"
        hits.append(
            Hit(
                kind="basis_bybit",
                symbol=sym,
                score=abs(basis),
                detail=f"basis {basis:+.3f}% · {side}",
                metrics={
                    "basis_pct": round(basis, 4),
                    "perp_mid": mid_p,
                    "spot_mid": mid_s,
                    "quote_volume": round(qv, 0),
                },
            )
        )

    # --- 2) Extreme Bybit funding ---
    for sym, fr in bybit_fr.items():
        if not sym.endswith("/USDT:USDT"):
            continue
        st = bybit_swap.get(sym) or {}
        qv = float((st.get("quoteVolume") or 0) or 0)
        if qv < min_quote_vol:
            continue
        fr_pct = fr * 100.0  # per 8h typically
        if abs(fr_pct) < min_funding_pct:
            continue
        side = "longs pay shorts" if fr > 0 else "shorts pay longs"
        hits.append(
            Hit(
                kind="funding_bybit",
                symbol=sym,
                score=abs(fr_pct) * 2.0,  # weight a bit
                detail=f"funding {fr_pct:+.4f}% /8h · {side}",
                metrics={
                    "funding_pct_8h": round(fr_pct, 5),
                    "quote_volume": round(qv, 0),
                },
            )
        )

    # --- 3) Cross-exchange perp mid Bybit vs Binance ---
    for sym, st in bybit_swap.items():
        if not sym.endswith("/USDT:USDT"):
            continue
        # Binance USDM uses BTC/USDT:USDT too in ccxt
        bn = binance_t.get(sym) or binance_t.get(sym.replace(":USDT", ""))
        mid_b = _mid(st)
        mid_n = _mid(bn)
        if mid_b is None or mid_n is None:
            continue
        qv = float((st.get("quoteVolume") or 0) or 0)
        qv2 = float((bn.get("quoteVolume") or 0) or 0) if bn else 0
        if min(qv, qv2 or qv) < min_quote_vol * 0.5:
            continue
        spread = _pct(mid_b, mid_n)  # bybit vs binance
        # Ignore broken matches (different contracts / stale symbols).
        if abs(spread) < min_xex_pct or abs(spread) > 15.0:
            continue
        # Relative price sanity: mids should be same order of magnitude.
        ratio = max(mid_b, mid_n) / min(mid_b, mid_n)
        if ratio > 1.2:
            continue
        side = "Bybit>Binance" if spread > 0 else "Bybit<Binance"
        hits.append(
            Hit(
                kind="xex_perp",
                symbol=sym,
                score=abs(spread) * 1.5,
                detail=f"perp mid {spread:+.3f}% · {side}",
                metrics={
                    "spread_pct": round(spread, 4),
                    "bybit_mid": mid_b,
                    "binance_mid": mid_n,
                    "bybit_qv": round(qv, 0),
                    "binance_qv": round(qv2, 0),
                },
            )
        )

    # --- 4) Funding divergence Bybit vs Binance ---
    for sym, fr_b in bybit_fr.items():
        fr_n = binance_fr.get(sym)
        if fr_n is None:
            continue
        st = bybit_swap.get(sym) or {}
        qv = float((st.get("quoteVolume") or 0) or 0)
        if qv < min_quote_vol:
            continue
        div = (fr_b - fr_n) * 100.0
        if abs(div) < min_fund_div_pct:
            continue
        hits.append(
            Hit(
                kind="funding_div",
                symbol=sym,
                score=abs(div) * 3.0,
                detail=(
                    f"funding d {div:+.4f} pp · "
                    f"Bybit {fr_b*100:+.4f}% vs Binance {fr_n*100:+.4f}%"
                ),
                metrics={
                    "delta_pp": round(div, 5),
                    "bybit_funding_pct": round(fr_b * 100, 5),
                    "binance_funding_pct": round(fr_n * 100, 5),
                    "quote_volume": round(qv, 0),
                },
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)

    by_kind: dict[str, list[Hit]] = {}
    for h in hits:
        by_kind.setdefault(h.kind, []).append(h)

    top_by_kind = {
        k: [asdict(x) for x in v[:top_n]] for k, v in by_kind.items()
    }
    # Unique symbols ranked by best score across kinds
    best: dict[str, Hit] = {}
    for h in hits:
        cur = best.get(h.symbol)
        if cur is None or h.score > cur.score:
            best[h.symbol] = h
    ranked = sorted(best.values(), key=lambda h: h.score, reverse=True)

    return {
        "scanned_at": _now_iso(),
        "elapsed_sec": round(time.time() - t0, 2),
        "thresholds": {
            "min_basis_pct": min_basis_pct,
            "min_funding_pct": min_funding_pct,
            "min_xex_pct": min_xex_pct,
            "min_fund_div_pct": min_fund_div_pct,
            "min_quote_vol": min_quote_vol,
        },
        "counts": {k: len(v) for k, v in by_kind.items()},
        "top_symbols": [asdict(h) for h in ranked[:top_n]],
        "by_kind": top_by_kind,
        "note": (
            "Public snapshots only. After fees/slippage many 'hits' are not tradable. "
            "Video topic: inefficiencies / arbitrage — this approximates basis, funding, x-ex spreads."
        ),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--min-basis", type=float, default=0.12, help="|spot-perp| %%")
    p.add_argument("--min-funding", type=float, default=0.05, help="|funding| %% per 8h")
    p.add_argument("--min-xex", type=float, default=0.20, help="|Bybit-Binance mid| %%")
    p.add_argument("--min-fund-div", type=float, default=0.03, help="|funding Bybit-Binance| pp")
    p.add_argument("--min-vol", type=float, default=500_000, help="min quote volume USDT/24h")
    p.add_argument("--top", type=int, default=25)
    p.add_argument(
        "--out",
        type=Path,
        default=ROOT / "simulation" / "results" / "market_inefficiencies" / "latest.json",
    )
    args = p.parse_args()

    # Force UTF-8 on Windows consoles.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    report = scan(
        min_basis_pct=args.min_basis,
        min_funding_pct=args.min_funding,
        min_xex_pct=args.min_xex,
        min_fund_div_pct=args.min_fund_div,
        min_quote_vol=args.min_vol,
        top_n=args.top,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Scanned at {report['scanned_at']} ({report['elapsed_sec']}s)")
    print(f"Counts: {report['counts']}")
    print(f"Saved: {args.out}")
    print()
    print("=== TOP inefficiencies (unique pairs) ===")
    for i, h in enumerate(report["top_symbols"], 1):
        print(f"{i:2d}. [{h['kind']}] {h['symbol']}  score={h['score']:.3f}  {h['detail']}")
    print()
    for kind, title in (
        ("basis_bybit", "Spot–perp basis (Bybit)"),
        ("funding_bybit", "Extreme funding (Bybit)"),
        ("xex_perp", "Cross-ex perp mid (Bybit↔Binance)"),
        ("funding_div", "Funding divergence (Bybit↔Binance)"),
    ):
        rows = report["by_kind"].get(kind) or []
        if not rows:
            continue
        print(f"--- {title} ---")
        for h in rows[:15]:
            print(f"  {h['symbol']}: {h['detail']}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

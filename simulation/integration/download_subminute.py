#!/usr/bin/env python3
"""Download sub-minute (1s) OHLCV from Bybit public trade archives."""
from __future__ import annotations

import argparse
import gzip
import json
import io
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

ARCHIVE_BASE = "https://public.bybit.com/trading"


def root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_manifest() -> dict:
    return json.loads((root() / "simulation" / "config" / "manifest.json").read_text(encoding="utf-8"))


def parse_timerange(timerange: str) -> tuple[datetime, datetime]:
    start_s, end_s = timerange.split("-")
    start = datetime.strptime(start_s, "%Y%m%d").replace(tzinfo=UTC)
    end = datetime.strptime(end_s, "%Y%m%d").replace(hour=23, minute=59, second=59, tzinfo=UTC)
    return start, end


def normalize_pair(pair: str) -> str:
    p = pair.strip()
    if p.endswith(":USD") and ":USDT" not in p:
        return p.replace(":USD", ":USDT")
    return p


def pair_to_symbol(pair: str) -> str:
    """SOL/USDT:USDT -> SOLUSDT"""
    base = pair.split("/")[0]
    return f"{base}USDT"


def pair_to_filename(pair: str) -> str:
    return pair.replace("/", "_").replace(":", "_")


def daterange(start: datetime, end: datetime):
    cur = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end_day:
        yield cur
        cur += timedelta(days=1)


def download_day_csv(symbol: str, day: datetime, timeout: float = 60.0) -> pd.DataFrame | None:
    day_s = day.strftime("%Y-%m-%d")
    url = f"{ARCHIVE_BASE}/{symbol}/{symbol}{day_s}.csv.gz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = gzip.decompress(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    df = pd.read_csv(io.BytesIO(raw))
    if df.empty:
        return None
    # timestamp column is seconds with fraction
    df["date"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.rename(columns={"size": "amount"})
    return df[["date", "price", "amount"]]


def trades_df_to_1s(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    trades = trades.set_index("date").sort_index()
    ohlcv = trades["price"].resample("1s").ohlc()
    vol = trades["amount"].resample("1s").sum()
    out = ohlcv.join(vol.rename("volume"))
    out = out.dropna(subset=["close"])
    out["volume"] = out["volume"].fillna(0.0)
    return out.reset_index()


def resample_higher(df_1s: pd.DataFrame, rule: str) -> pd.DataFrame:
    idx = df_1s.set_index("date")
    out = idx.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    return out.reset_index()


def save_feather(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_feather(path)


def download_pair(
    pair: str,
    datadir: Path,
    start: datetime,
    end: datetime,
) -> dict:
    pair = normalize_pair(pair)
    symbol = pair_to_symbol(pair)
    safe = pair_to_filename(pair)
    out_1s = datadir / "bybit" / f"{safe}-1s-futures.feather"
    print(f"Downloading {pair} ({symbol}) from public.bybit.com …")

    existing = pd.read_feather(out_1s) if out_1s.is_file() else None
    if existing is not None and not existing.empty:
        existing["date"] = pd.to_datetime(existing["date"], utc=True)
        have_start = existing["date"].min()
        have_end = existing["date"].max()
        if have_start <= start and have_end >= end:
            return {
                "pair": pair,
                "symbol": symbol,
                "days": 0,
                "candles_1s": len(existing),
                "skipped": "already covers timerange",
                "path_1s": str(out_1s),
            }
        # Only fetch missing edges
        fetch_ranges: list[tuple[datetime, datetime]] = []
        if start < have_start:
            fetch_ranges.append((start, min(end, have_start - timedelta(seconds=1))))
        if end > have_end:
            fetch_ranges.append((max(start, have_end + timedelta(seconds=1)), end))
    else:
        fetch_ranges = [(start, end)]

    chunks: list[pd.DataFrame] = []
    days_ok = 0
    for range_start, range_end in fetch_ranges:
        for day in daterange(range_start, range_end):
            df = download_day_csv(symbol, day)
            if df is None:
                print(f"  skip {day.date()} (no file)")
                continue
            chunks.append(df)
            days_ok += 1
            print(f"  {day.date()}: {len(df)} trades")

    trade_count = 0
    if chunks:
        all_trades = pd.concat(chunks, ignore_index=True)
        trade_count = len(all_trades)
        df_new = trades_df_to_1s(all_trades)
        df_new = df_new[(df_new["date"] >= start) & (df_new["date"] <= end)]
        if existing is not None and not existing.empty:
            df_1s = (
                pd.concat([df_new, existing], ignore_index=True)
                .drop_duplicates(subset=["date"])
                .sort_values("date")
            )
            df_1s = df_1s[(df_1s["date"] >= start) & (df_1s["date"] <= end)]
        else:
            df_1s = df_new
    elif existing is not None and not existing.empty:
        df_1s = existing[(existing["date"] >= start) & (existing["date"] <= end)]
    else:
        return {"pair": pair, "error": "no archive files found", "days": 0}
    save_feather(df_1s, out_1s)

    df_1m = resample_higher(df_1s, "1min")
    out_1m = datadir / "bybit" / f"{safe}-1m-futures.feather"
    save_feather(df_1m, out_1m)

    df_5m = resample_higher(df_1s, "5min")
    out_5m = datadir / "bybit" / f"{safe}-5m-futures.feather"
    save_feather(df_5m, out_5m)

    return {
        "pair": pair,
        "symbol": symbol,
        "days": days_ok,
        "trades": trade_count,
        "candles_1s": len(df_1s),
        "candles_1m": len(df_1m),
        "candles_5m": len(df_5m),
        "path_1s": str(out_1s),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Download 1s candles from Bybit public archives")
    parser.add_argument("--pairs", nargs="*", default=None)
    parser.add_argument("--timerange", default=None)
    parser.add_argument("--datadir", type=Path, default=None)
    args = parser.parse_args()

    cfg = load_manifest()
    pairs = args.pairs or cfg.get("player_pairs") or []
    if not pairs:
        print("No pairs configured")
        return 1

    timerange = args.timerange or cfg.get("player_timerange") or cfg.get("timerange", "20260613-20260628")
    start, end = parse_timerange(timerange)
    datadir = args.datadir or (root() / cfg["ctengine_datadir"])
    datadir.mkdir(parents=True, exist_ok=True)

    summary = []
    for pair in pairs:
        try:
            info = download_pair(pair, datadir, start, end)
            summary.append(info)
            if info.get("candles_1s"):
                print(f"  OK {pair}: {info['candles_1s']} x 1s")
            else:
                print(f"  WARN {pair}: {info.get('error', 'empty')}")
        except Exception as exc:
            print(f"  FAIL {pair}: {exc}")
            summary.append({"pair": pair, "error": str(exc)})

    report_path = root() / "simulation" / "data" / "subminute_download.json"
    report_path.write_text(
        json.dumps({"timerange": timerange, "source": ARCHIVE_BASE, "pairs": summary}, indent=2),
        encoding="utf-8",
    )
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

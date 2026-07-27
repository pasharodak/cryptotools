"""Load historical OHLCV from Freqtrade datadir (feather/json) or download via ccxt."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd


class HistoricalDatastore:
    """Reads Freqtrade candle files and serves slices by simulated clock."""

    def __init__(self, datadir: Path, exchange: str = "bybit", candle_type: str = "futures"):
        self.datadir = Path(datadir)
        self.exchange = exchange
        self.candle_type = candle_type
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}

    def _candle_search_dirs(self) -> list[Path]:
        """Freqtrade may store futures OHLCV under futures/ or exchange name."""
        candidates = [
            self.datadir / "futures",
            self.datadir / self.exchange,
            self.datadir,
        ]
        seen: set[Path] = set()
        out: list[Path] = []
        for d in candidates:
            if d.is_dir() and d not in seen:
                seen.add(d)
                out.append(d)
        return out

    def _candle_paths(self, pair: str, timeframe: str) -> list[Path]:
        """Collect candle files across futures/ and exchange/ subdirs.

        Match only exact pair stems (e.g. BTC_USDT_USDT-5m-...), never substring
        hits like ETHBTC when looking up BTC.
        """
        safe = pair.replace("/", "_").replace(":", "_")
        # Freqtrade futures naming: BASE_QUOTE_SETTLE-tf-candletype.feather
        exact_names = {
            f"{pair}-{timeframe}-{self.candle_type}.feather",
            f"{pair.replace('/', '')}-{timeframe}-{self.candle_type}.feather",
            f"{safe}-{timeframe}-{self.candle_type}.feather",
            f"{pair}-{timeframe}.feather",
            f"{safe}-{timeframe}.feather",
        }
        # Allowed stem prefixes before "-{timeframe}"
        allowed_stems = {safe, pair.replace("/", ""), pair}
        found: list[Path] = []
        seen_files: set[Path] = set()
        for base in self._candle_search_dirs():
            for name in exact_names:
                p = base / name
                if p.is_file() and p not in seen_files:
                    found.append(p)
                    seen_files.add(p)
            needle = f"-{timeframe}-"
            for p in base.glob(f"*{needle}*.feather"):
                if p in seen_files:
                    continue
                stem_pair = p.name.split(needle, 1)[0]
                if stem_pair in allowed_stems:
                    found.append(p)
                    seen_files.add(p)
        return found

    def _read_candle_frame(self, path: Path) -> pd.DataFrame:
        df = pd.read_feather(path)
        if "date" not in df.columns:
            return df.sort_index() if isinstance(df.index, pd.DatetimeIndex) else df
        if pd.api.types.is_numeric_dtype(df["date"]):
            df["date"] = pd.to_datetime(df["date"], unit="ms", utc=True)
        else:
            df["date"] = pd.to_datetime(df["date"], utc=True)
        return df.set_index("date").sort_index()

    def _pick_best_candle_path(self, paths: list[Path]) -> Path:
        """Prefer OHLCV over mark/funding, then newest end, then widest history."""
        if len(paths) == 1:
            return paths[0]

        def score(p: Path) -> tuple:
            name = p.name
            is_mark = "mark" in name or "funding" in name
            prefer = 0 if is_mark else 1
            # Prefer futures/ over exchange mirror when both exist
            in_futures = 1 if "futures" in p.parts else 0
            try:
                df = self._read_candle_frame(p)
            except Exception:
                return (prefer, in_futures, -1, -1, 0)
            if df.empty:
                return (prefer, in_futures, -1, -1, 0)
            # Newest coverage first (critical when an older longer file exists)
            return (prefer, in_futures, int(df.index[-1].value), len(df), -int(df.index[0].value))

        return max(paths, key=score)

    def has_pair(self, pair: str, timeframe: str = "5m") -> bool:
        return bool(self._candle_paths(pair, timeframe))

    def load(self, pair: str, timeframe: str = "5m") -> pd.DataFrame:
        key = (pair, timeframe)
        if key in self._cache:
            return self._cache[key]
        paths = self._candle_paths(pair, timeframe)
        if not paths:
            raise FileNotFoundError(f"No candles for {pair} {timeframe} in {self.datadir}")
        path = self._pick_best_candle_path(paths)
        df = self._read_candle_frame(path)
        self._cache[key] = df
        return df

    def list_pairs(self, timeframe: str = "5m") -> list[str]:
        pairs: set[str] = set()
        for base in self._candle_search_dirs():
            for p in base.glob(f"*-{timeframe}-*.feather"):
                name = p.stem
                parts = name.split(f"-{timeframe}-")
                if parts:
                    sym = parts[0].replace("_", "/").replace("/USDT/USDT", "/USDT:USDT")
                    if ":" not in sym and "/USDT" in sym:
                        sym = sym.replace("/USDT", "/USDT:USDT")
                    pairs.add(sym)
        return sorted(pairs)

    def kline_at(
        self,
        pair: str,
        timeframe: str,
        ts_ms: int,
    ) -> dict[str, Any] | None:
        df = self.load(pair, timeframe)
        ts = pd.Timestamp(ts_ms, unit="ms", tz=UTC)
        idx = df.index.get_indexer([ts], method="pad")
        if idx[0] < 0:
            return None
        row = df.iloc[idx[0]]
        t = int(df.index[idx[0]].timestamp() * 1000)
        return {
            "symbol": pair.replace("/", "").replace(":", ""),
            "interval": timeframe,
            "startTime": t,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }

    def export_manifest(self, path: Path) -> None:
        manifest = {
            "exchange": self.exchange,
            "pairs": self.list_pairs(),
            "datadir": str(self.datadir),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def list_timeframes(self, pair: str | None = None) -> list[str]:
        tfs: set[str] = set()
        for base in self._candle_search_dirs():
            for p in base.glob("*.feather"):
                stem = p.stem
                if pair:
                    safe = pair.replace("/", "_").replace(":", "_")
                    if safe not in stem and pair.split("/")[0] not in stem:
                        continue
                for part in stem.split("-"):
                    if part.endswith("s") and part[:-1].isdigit():
                        tfs.add(part)
                    elif part.endswith("m") and part[:-1].isdigit():
                        tfs.add(part)
        order = {"1s": 0, "5s": 1, "15s": 2, "1m": 3, "5m": 4, "15m": 5, "1h": 6}
        return sorted(tfs, key=lambda x: order.get(x, 99))

    def pair_range(self, pair: str, timeframe: str = "1s") -> dict[str, int | None]:
        try:
            df = self.load(pair, timeframe)
        except FileNotFoundError:
            return {"start_ms": None, "end_ms": None, "count": 0}
        if df.empty:
            return {"start_ms": None, "end_ms": None, "count": 0}
        return {
            "start_ms": int(df.index[0].timestamp() * 1000),
            "end_ms": int(df.index[-1].timestamp() * 1000),
            "count": len(df),
        }

    def chart_slice(
        self,
        pair: str,
        timeframe: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        df = self.load(pair, timeframe)
        if start_ms is not None:
            ts = pd.Timestamp(start_ms, unit="ms", tz=UTC)
            df = df[df.index >= ts]
        if end_ms is not None:
            ts = pd.Timestamp(end_ms, unit="ms", tz=UTC)
            df = df[df.index <= ts]
        if len(df) > limit:
            df = df.iloc[:limit]
        rows = []
        for idx, row in df.iterrows():
            rows.append(
                {
                    "time": int(idx.timestamp()),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
            )
        return rows

    def clear_cache(self) -> None:
        self._cache.clear()

"""Historical scanner replay — same logic/interval as live ranging + strategy scanners."""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
from simulation.paths import SCRIPTS, USER_DATA  # noqa: E402

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scan_ranging_pairs import analyze_ranging  # noqa: E402
from scan_strategy_pairs import compute_pair_metrics, score_strategy  # noqa: E402

from .datastore import HistoricalDatastore

DEFAULT_SCAN_INTERVAL_MS = 30 * 60 * 1000  # live ranging/strategy scanners (30m)
SCAN_INTERVAL_MS = DEFAULT_SCAN_INTERVAL_MS  # backward compat
DEFAULT_SCAN_WORKERS = 0  # 0 = auto (cpu_count)


def resolve_scan_workers(workers: int | None = None) -> int:
    """Clamp scan worker count; 0/None picks logical CPU count (max 16)."""
    n = workers if workers is not None else DEFAULT_SCAN_WORKERS
    if n <= 0:
        n = os.cpu_count() or 4
    return max(1, min(int(n), 16))


def preload_ohlcv(ds: HistoricalDatastore, pairs: list[str], timeframe: str = "5m") -> dict[str, pd.DataFrame]:
    """Load all pair candles once — avoids repeated disk I/O during scan replay."""
    out: dict[str, pd.DataFrame] = {}
    missing = 0
    for pair in pairs:
        try:
            out[pair] = ds.load(pair, timeframe)
        except FileNotFoundError:
            missing += 1
    if missing:
        print(f"  scan preload: {len(out)}/{len(pairs)} pairs ({missing} missing OHLCV)", flush=True)
    return out


def load_json(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def pair_base(pair: str) -> str:
    return pair.split("/")[0]


def align_scan_start(ms: int, interval_ms: int | None = None) -> int:
    """Align to scan-interval boundary (UTC)."""
    interval_ms = interval_ms if interval_ms is not None else DEFAULT_SCAN_INTERVAL_MS
    interval_min = max(1, interval_ms // 60_000)
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    total_min = dt.hour * 60 + dt.minute
    aligned_min = (total_min // interval_min) * interval_min
    aligned = dt.replace(hour=aligned_min // 60, minute=aligned_min % 60, second=0, microsecond=0)
    return int(aligned.timestamp() * 1000)


def iter_scan_times(start_ms: int, end_ms: int, interval_ms: int | None = None) -> list[int]:
    interval_ms = interval_ms if interval_ms is not None else DEFAULT_SCAN_INTERVAL_MS
    times: list[int] = []
    t = align_scan_start(start_ms, interval_ms)
    while t <= end_ms:
        times.append(t)
        t += interval_ms
    return times


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return out.dropna(subset=["close"])


def slice_at(df: pd.DataFrame, ts_ms: int, lookback: int) -> pd.DataFrame:
    ts = pd.Timestamp(ts_ms, unit="ms", tz=UTC)
    sub = df[df.index <= ts]
    return sub.tail(lookback + 30).copy()


def load_scan_configs(root: Path) -> tuple[dict, dict, dict]:
    ranging = load_json(USER_DATA / "ranging_scan_config.json")
    strategy = load_json(USER_DATA / "strategy_scan_config.json")
    enabled = load_json(USER_DATA / "enabled_strategies.json").get("enabled", {})
    player_scan = load_json(root / "simulation" / "config" / "sim_player_scan.json")
    if player_scan.get("ranging"):
        ranging = {**ranging, **player_scan["ranging"]}
    if player_scan.get("strategy"):
        strategy = {**strategy, **player_scan["strategy"]}
    sim_ov = load_json(root / "simulation" / "config" / "sim_scan_overrides.json")
    if sim_ov.get("ranging"):
        ranging = {**ranging, **sim_ov["ranging"]}
    if sim_ov.get("strategy"):
        strategy = {**strategy, **sim_ov["strategy"]}
    return ranging, strategy, enabled


def grid_scan_at(
    ds: HistoricalDatastore | None,
    pairs: list[str],
    ts_ms: int,
    cfg: dict,
    blacklist: set[str],
    *,
    ohlcv: dict[str, pd.DataFrame] | None = None,
) -> list[dict[str, Any]]:
    lookback = int(cfg.get("lookback_candles", 96))
    min_ratio = float(cfg.get("min_ranging_ratio", 0.55))
    max_pairs = int(cfg.get("max_pairs", 5))
    priority_bases = set(cfg.get("priority_bases") or [])
    boost = float(cfg.get("priority_score_boost", 1.25))
    exclude_bases = set(cfg.get("exclude_bases") or [])

    results: list[dict[str, Any]] = []
    for pair in pairs:
        if pair in blacklist or pair_base(pair) in exclude_bases:
            continue
        try:
            if ohlcv is not None:
                full = ohlcv.get(pair)
            elif ds is not None:
                full = ds.load(pair, "5m")
            else:
                continue
            if full is None:
                continue
            df5 = slice_at(full, ts_ms, lookback)
        except FileNotFoundError:
            continue
        if len(df5) < lookback // 2:
            continue
        df_htf = resample_ohlcv(df5, "15min")
        metrics = analyze_ranging(df5, cfg, df_htf=df_htf if len(df_htf) > 20 else None)
        if metrics is None or metrics.get("ranging_ratio", 0) < min_ratio:
            continue
        score = float(metrics["score"])
        if pair_base(pair) in priority_bases:
            score *= boost
        results.append({"pair": pair, "score": score, **metrics})

    results.sort(key=lambda x: x["score"], reverse=True)
    selected = results[:max_pairs]
    return selected


def strategy_scan_at(
    ds: HistoricalDatastore | None,
    pairs: list[str],
    ts_ms: int,
    cfg: dict,
    enabled: dict[str, bool],
    *,
    ohlcv: dict[str, pd.DataFrame] | None = None,
) -> list[dict[str, Any]]:
    lookback = int(cfg.get("lookback_candles", 48))
    profiles = cfg.get("strategy_profiles") or {}
    max_pairs = int(cfg.get("max_pairs", 12))
    exclude_bases = set(cfg.get("exclude_bases") or [])

    best: dict[str, dict] = {}
    for pair in pairs:
        if pair_base(pair) in exclude_bases:
            continue
        try:
            if ohlcv is not None:
                full = ohlcv.get(pair)
            elif ds is not None:
                full = ds.load(pair, "5m")
            else:
                continue
            if full is None:
                continue
            df5 = slice_at(full, ts_ms, max(lookback, 210))
        except FileNotFoundError:
            continue
        metrics = compute_pair_metrics(
            df5,
            bb_period=int(cfg.get("bb_period", 20)),
            adx_period=int(cfg.get("adx_period", 14)),
            rsi_period=int(cfg.get("rsi_period", 14)),
        )
        if metrics is None:
            continue
        for sid, on in enabled.items():
            if not on or sid not in profiles:
                continue
            sc = score_strategy(sid, metrics, profiles[sid])
            if sc is None:
                continue
            prev = best.get(pair)
            if prev is None or sc > prev["score"]:
                best[pair] = {"pair": pair, "score": sc, "strategy_id": sid}

    ranked = sorted(best.values(), key=lambda x: x["score"], reverse=True)[:max_pairs]
    return ranked


def filter_hits_by_recent_pnl(
    hits: list[dict[str, Any]],
    trades_by_pair: dict[str, list[dict]] | None,
    ts_ms: int,
    *,
    window_ms: int = 7 * 24 * 3600 * 1000,
    min_loss_usdt: float = 0.0,
    max_consecutive_losses: int = 0,
) -> list[dict[str, Any]]:
    """Drop pairs with negative closed-trade PnL or loss streak in the lookback window."""
    if not trades_by_pair:
        return hits
    kept: list[dict[str, Any]] = []
    for hit in hits:
        pair = hit["pair"]
        closed = sorted(
            [
                t
                for t in trades_by_pair.get(pair, [])
                if ts_ms - window_ms <= t.get("close_ms", 0) < ts_ms
            ],
            key=lambda t: t.get("close_ms", 0),
        )
        if not closed:
            kept.append(hit)
            continue
        pnl_sum = sum(float(t.get("profit_abs") or 0) for t in closed)
        if min_loss_usdt <= 0 and pnl_sum < 0:
            continue
        if min_loss_usdt > 0 and pnl_sum < -min_loss_usdt:
            continue
        if max_consecutive_losses > 0:
            tail = closed[-max_consecutive_losses:]
            if len(tail) >= max_consecutive_losses and all(float(t.get("profit_abs") or 0) < 0 for t in tail):
                continue
        kept.append(hit)
    return kept


def resolve_scan_interval_ms(root: Path, override_ms: int | None = None) -> int:
    if override_ms is not None:
        return int(override_ms)
    player = load_json(root / "simulation" / "config" / "sim_player_scan.json")
    if player.get("scan_interval_minutes"):
        return int(player["scan_interval_minutes"]) * 60 * 1000
    return DEFAULT_SCAN_INTERVAL_MS


def _scan_times_chunk(
    chunk_id: int,
    scan_times: list[int],
    pairs: list[str],
    ohlcv: dict[str, pd.DataFrame],
    ranging_cfg: dict,
    strategy_cfg: dict,
    enabled: dict[str, bool],
    bl: set[str],
    *,
    use_pnl_gate: bool,
    grid_trades_by_pair: dict[str, list[dict]] | None,
    pnl_window_ms: int,
    pnl_min_loss: float,
    pnl_max_streak: int,
    rolling: bool,
) -> dict[str, Any]:
    """Process a contiguous slice of scan timestamps (thread-worker safe)."""
    grid_arms: dict[str, int | None] = {p: None for p in pairs}
    strategy_arms: dict[str, int | None] = {p: None for p in pairs}
    grid_timeline: list[tuple[int, frozenset[str]]] = []
    strategy_timeline: list[tuple[int, frozenset[str]]] = []
    events: list[dict[str, Any]] = []

    for ts_ms in scan_times:
        grid_hits = grid_scan_at(
            None, pairs, ts_ms, ranging_cfg, bl, ohlcv=ohlcv
        )
        if use_pnl_gate:
            grid_hits = filter_hits_by_recent_pnl(
                grid_hits,
                grid_trades_by_pair,
                ts_ms,
                window_ms=pnl_window_ms,
                min_loss_usdt=pnl_min_loss,
                max_consecutive_losses=pnl_max_streak,
            )
        grid_active = frozenset(h["pair"] for h in grid_hits)
        grid_timeline.append((ts_ms, grid_active))
        for hit in grid_hits:
            p = hit["pair"]
            if grid_arms[p] is None:
                grid_arms[p] = ts_ms
                events.append(
                    {
                        "ms": ts_ms,
                        "scenario_id": "grid_improved",
                        "pair": p,
                        "action": "armed",
                        "score": hit["score"],
                        "adx": hit.get("adx"),
                        "rolling": rolling,
                    }
                )

        strat_hits = strategy_scan_at(
            None, pairs, ts_ms, strategy_cfg, enabled, ohlcv=ohlcv
        )
        strat_active = frozenset(h["pair"] for h in strat_hits)
        strategy_timeline.append((ts_ms, strat_active))
        for hit in strat_hits:
            p = hit["pair"]
            if strategy_arms[p] is None:
                strategy_arms[p] = ts_ms
                events.append(
                    {
                        "ms": ts_ms,
                        "scenario_id": "strategy_improved",
                        "pair": p,
                        "action": "armed",
                        "score": hit["score"],
                        "strategy_id": hit.get("strategy_id"),
                        "rolling": rolling,
                    }
                )

    return {
        "chunk_id": chunk_id,
        "grid_arms": grid_arms,
        "strategy_arms": strategy_arms,
        "grid_timeline": grid_timeline,
        "strategy_timeline": strategy_timeline,
        "events": events,
    }


def _merge_scan_chunks(chunks: list[dict[str, Any]], pairs: list[str]) -> tuple[
    dict[str, int | None],
    dict[str, int | None],
    list[tuple[int, frozenset[str]]],
    list[tuple[int, frozenset[str]]],
    list[dict[str, Any]],
]:
    grid_arms: dict[str, int | None] = {p: None for p in pairs}
    strategy_arms: dict[str, int | None] = {p: None for p in pairs}
    grid_timeline: list[tuple[int, frozenset[str]]] = []
    strategy_timeline: list[tuple[int, frozenset[str]]] = []
    events: list[dict[str, Any]] = []

    for chunk in sorted(chunks, key=lambda c: c["chunk_id"]):
        grid_timeline.extend(chunk["grid_timeline"])
        strategy_timeline.extend(chunk["strategy_timeline"])
        events.extend(chunk["events"])
        for p in pairs:
            gts = chunk["grid_arms"].get(p)
            if gts is not None and (grid_arms[p] is None or gts < grid_arms[p]):
                grid_arms[p] = gts
            sts = chunk["strategy_arms"].get(p)
            if sts is not None and (strategy_arms[p] is None or sts < strategy_arms[p]):
                strategy_arms[p] = sts

    events.sort(key=lambda e: (e.get("ms") or 0, e.get("pair") or ""))
    return grid_arms, strategy_arms, grid_timeline, strategy_timeline, events


def _chunk_scan_times(scan_times: list[int], workers: int) -> list[list[int]]:
    n = len(scan_times)
    if n == 0:
        return []
    workers = min(workers, n)
    size = (n + workers - 1) // workers
    return [scan_times[i : i + size] for i in range(0, n, size)]


_MP_OHLCV: dict[str, pd.DataFrame] = {}


def _scan_worker_init(datadir: str, pairs: list[str]) -> None:
    """Process-pool initializer: load OHLCV once per worker."""
    global _MP_OHLCV
    ds = HistoricalDatastore(Path(datadir), exchange="bybit")
    _MP_OHLCV = preload_ohlcv(ds, pairs)


def _scan_chunk_task(payload: dict[str, Any]) -> dict[str, Any]:
    return _scan_times_chunk(
        payload["chunk_id"],
        payload["scan_times"],
        payload["pairs"],
        _MP_OHLCV,
        payload["ranging_cfg"],
        payload["strategy_cfg"],
        payload["enabled"],
        set(payload["bl"]),
        use_pnl_gate=payload["use_pnl_gate"],
        grid_trades_by_pair=payload.get("grid_trades_by_pair"),
        pnl_window_ms=payload["pnl_window_ms"],
        pnl_min_loss=payload["pnl_min_loss"],
        pnl_max_streak=payload["pnl_max_streak"],
        rolling=payload["rolling"],
    )


def _run_parallel_scan_chunks(
    *,
    datadir: Path,
    pairs: list[str],
    chunks: list[list[int]],
    n_workers: int,
    ranging_cfg: dict,
    strategy_cfg: dict,
    enabled: dict[str, bool],
    bl: set[str],
    use_pnl_gate: bool,
    grid_trades_by_pair: dict[str, list[dict]] | None,
    pnl_window_ms: int,
    pnl_min_loss: float,
    pnl_max_streak: int,
    rolling: bool,
) -> list[dict[str, Any]]:
    """Run scan chunks in separate processes (true CPU parallelism)."""
    tasks = [
        {
            "chunk_id": i,
            "scan_times": chunk,
            "pairs": pairs,
            "ranging_cfg": ranging_cfg,
            "strategy_cfg": strategy_cfg,
            "enabled": enabled,
            "bl": sorted(bl),
            "use_pnl_gate": use_pnl_gate,
            "grid_trades_by_pair": grid_trades_by_pair,
            "pnl_window_ms": pnl_window_ms,
            "pnl_min_loss": pnl_min_loss,
            "pnl_max_streak": pnl_max_streak,
            "rolling": rolling,
        }
        for i, chunk in enumerate(chunks)
    ]
    parts: list[dict[str, Any]] = []
    done = 0
    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_scan_worker_init,
        initargs=(str(datadir), pairs),
    ) as pool:
        futs = {pool.submit(_scan_chunk_task, task): task["chunk_id"] for task in tasks}
        for fut in as_completed(futs):
            parts.append(fut.result())
            done += 1
            if done % max(1, len(chunks) // 4) == 0 or done == len(chunks):
                print(f"    scan chunks {done}/{len(chunks)}", flush=True)
    return parts


def build_arm_schedules(
    ds: HistoricalDatastore,
    pairs: list[str],
    start_ms: int,
    end_ms: int,
    root: Path,
    grid_blacklist: set[str] | None = None,
    grid_trades_by_pair: dict[str, list[dict]] | None = None,
    scan_interval_ms: int | None = None,
    *,
    workers: int | None = None,
) -> dict[str, Any]:
    """Return scan timelines (rolling whitelist), legacy arms, and events."""
    interval_ms = resolve_scan_interval_ms(root, scan_interval_ms)
    ranging_cfg, strategy_cfg, enabled = load_scan_configs(root)
    rolling = bool(ranging_cfg.get("rolling_whitelist", True))
    pnl_window_ms = int(ranging_cfg.get("pnl_circuit_window_ms", 7 * 24 * 3600 * 1000))
    use_pnl_gate = bool(ranging_cfg.get("pnl_circuit_breaker", True))
    pnl_min_loss = float(ranging_cfg.get("pnl_circuit_min_loss_usdt", 0.0))
    pnl_max_streak = int(ranging_cfg.get("pnl_circuit_max_loss_streak", 0))
    grace_scans = int(ranging_cfg.get("whitelist_grace_scans", 2))
    trade_loss_streak = int(ranging_cfg.get("trade_skip_loss_streak", 0))
    trade_loss_window_ms = int(ranging_cfg.get("trade_loss_window_ms", 7 * 24 * 3600 * 1000))
    trade_skip_min_cum_loss = float(ranging_cfg.get("trade_skip_min_cum_loss_usdt", 0.0))
    bl = grid_blacklist or set()
    scan_times = iter_scan_times(start_ms, end_ms, interval_ms)
    n_workers = resolve_scan_workers(workers)

    t0 = time.perf_counter()
    ohlcv = preload_ohlcv(ds, pairs)
    preload_s = time.perf_counter() - t0

    if n_workers <= 1 or len(scan_times) < n_workers * 2:
        chunk = _scan_times_chunk(
            0,
            scan_times,
            pairs,
            ohlcv,
            ranging_cfg,
            strategy_cfg,
            enabled,
            bl,
            use_pnl_gate=use_pnl_gate,
            grid_trades_by_pair=grid_trades_by_pair,
            pnl_window_ms=pnl_window_ms,
            pnl_min_loss=pnl_min_loss,
            pnl_max_streak=pnl_max_streak,
            rolling=rolling,
        )
        grid_arms = chunk["grid_arms"]
        strategy_arms = chunk["strategy_arms"]
        grid_timeline = chunk["grid_timeline"]
        strategy_timeline = chunk["strategy_timeline"]
        events = chunk["events"]
    else:
        chunks = _chunk_scan_times(scan_times, n_workers)
        print(
            f"  scan replay: {len(scan_times)} steps · {len(pairs)} pairs · "
            f"{len(chunks)} chunks · {n_workers} workers (process pool)",
            flush=True,
        )
        parts = _run_parallel_scan_chunks(
            datadir=ds.datadir,
            pairs=pairs,
            chunks=chunks,
            n_workers=n_workers,
            ranging_cfg=ranging_cfg,
            strategy_cfg=strategy_cfg,
            enabled=enabled,
            bl=bl,
            use_pnl_gate=use_pnl_gate,
            grid_trades_by_pair=grid_trades_by_pair,
            pnl_window_ms=pnl_window_ms,
            pnl_min_loss=pnl_min_loss,
            pnl_max_streak=pnl_max_streak,
            rolling=rolling,
        )
        grid_arms, strategy_arms, grid_timeline, strategy_timeline, events = _merge_scan_chunks(parts, pairs)

    elapsed = time.perf_counter() - t0
    print(
        f"  scan done: {len(scan_times)} steps · preload {preload_s:.1f}s · total {elapsed:.1f}s · "
        f"workers={n_workers}",
        flush=True,
    )

    return {
        "scan_interval_ms": interval_ms,
        "scan_times": scan_times,
        "grid_arms": grid_arms,
        "strategy_arms": strategy_arms,
        "grid_timeline": grid_timeline,
        "strategy_timeline": strategy_timeline,
        "rolling_whitelist": rolling,
        "whitelist_grace_scans": grace_scans,
        "trade_skip_loss_streak": trade_loss_streak,
        "trade_skip_min_cum_loss_usdt": trade_skip_min_cum_loss,
        "trade_loss_window_ms": trade_loss_window_ms,
        "events": events,
    }

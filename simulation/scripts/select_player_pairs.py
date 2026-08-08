#!/usr/bin/env python3
"""Probe pool pairs and assign each to the best profitable strategy (no hardcoded symbols)."""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import patch_whitelist  # noqa: E402
from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402

PROBE_SCENARIOS: dict[str, dict] = {
    "live_grid": {
        "strategy": "SimVolatilityGridAggressive",
        "config": "simulation/config/backtest_grid_improved.json",
        "strategy_path": "simulation/strategies",
        "stake_key": "probe_stake_grid",
        "default_stake": 50,
        "priority": 3,
    },
    "live_grid_safe": {
        "strategy": "SimVolatilityGridPlayer",
        "config": "simulation/config/backtest_grid_improved.json",
        "strategy_path": "simulation/strategies",
        "stake_key": "probe_stake_grid_safe",
        "default_stake": 30,
        "priority": 2,
    },
    "lite_swing": {
        "strategy": "LiteSwingStrategy",
        "config": "simulation/config/backtest_lite_base.json",
        "strategy_path": "simulation/strategies",
        "stake_key": "probe_stake_lite",
        "default_stake": 12,
        "priority": 1,
    },
    "lite_arbitrage": {
        "strategy": "LiteArbitrageStrategy",
        "config": "simulation/config/backtest_lite_base.json",
        "strategy_path": "simulation/strategies",
        "stake_key": "probe_stake_lite",
        "default_stake": 12,
        "priority": 1,
    },
}


@dataclass
class PairAssignment:
    pairs: list[str] = field(default_factory=list)
    assignments: dict[str, list[str]] = field(default_factory=dict)
    probes: list[dict] = field(default_factory=list)
    ranking: list[dict] = field(default_factory=list)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def resample_5m(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return (
        df.resample("5min")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["close"])
    )


def pool_pairs(root: Path, pool: list[str] | None = None) -> list[str]:
    if pool:
        return pool
    pool_path = root / "simulation/config/player_pair_pool.json"
    return load_json(pool_path).get("pairs") or []


def pairs_with_data(root: Path, datadir: Path, pool: list[str]) -> list[str]:
    ds = HistoricalDatastore(datadir, exchange="bybit")
    out: list[str] = []
    for pair in pool:
        try:
            ds.load(pair, "5m")
            out.append(pair)
        except FileNotFoundError:
            try:
                ds.load(pair, "1s")
                out.append(pair)
            except FileNotFoundError:
                continue
    return out


def probe_pair(
    root: Path,
    pair: str,
    timerange: str,
    scenario_id: str,
    stake: int,
) -> float:
    meta = PROBE_SCENARIOS[scenario_id]
    sc = {
        "id": f"probe_{scenario_id}",
        "config": meta["config"],
        "strategy_path": meta["strategy_path"],
        "stake_usdt": stake,
    }
    safe = pair.replace("/", "_").replace(":", "_")
    runtime = root / "simulation/data/runtime" / f"probe_{scenario_id}_{safe}.json"
    patch_whitelist(root, meta["config"], [pair], runtime, scenario=sc)
    out_dir = root / "simulation/results/pair_pick"
    out_dir.mkdir(parents=True, exist_ok=True)
    strategy = meta["strategy"]
    cmd = [
        str(root / ".venv/Scripts/ctbot.exe"),
        "backtesting",
        "--config",
        str(runtime),
        "--strategy",
        strategy,
        "--strategy-path",
        str(root / meta["strategy_path"]),
        "--datadir",
        str(root / "simulation/data/ctengine"),
        "--timerange",
        timerange,
        "--export",
        "trades",
        "--export-filename",
        f"probe_{scenario_id}_{pair.split('/')[0]}",
        "--backtest-directory",
        str(out_dir),
        "--cache",
        "none",
    ]
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        return float("-inf")
    last = out_dir / ".last_result.json"
    if not last.is_file():
        return 0.0
    latest = json.loads(last.read_text(encoding="utf-8")).get("latest_backtest")
    zpath = out_dir / latest
    if not zpath.is_file():
        return 0.0
    with zipfile.ZipFile(zpath) as zf:
        js = next(n for n in zf.namelist() if n.endswith(".json") and "_config" not in n)
        data = json.loads(zf.read(js))
    trades = data.get("strategy", {}).get(strategy, {}).get("trades") or []
    return sum(float(t["profit_abs"]) for t in trades)


def assign_pairs(
    root: Path,
    datadir: Path,
    at_ms: int,
    pool: list[str] | None = None,
    profile: dict | None = None,
    *,
    use_cache: bool = True,
) -> PairAssignment:
    profile = profile or load_json(root / "simulation/config/player_profile.json")
    sel = profile.get("pair_selection", {})
    manifest = load_json(root / "simulation/config/manifest.json")
    timerange = manifest.get("player_timerange", "20260601-20260625")

    if sel.get("mode") == "static":
        static = manifest.get("player_pairs") or pool_pairs(root, pool)
        return PairAssignment(pairs=static, assignments={"all": static})

    pool = pairs_with_data(root, datadir, pool_pairs(root, pool))
    cache_path = root / "simulation/results/selected_player_pairs.json"
    if use_cache and sel.get("use_probe_cache", True) and cache_path.is_file():
        cached = load_json(cache_path)
        if _cache_valid(cached, at_ms, pool):
            return PairAssignment(
                pairs=cached.get("pairs") or [],
                assignments=cached.get("assignments") or {},
                probes=cached.get("probes") or [],
                ranking=cached.get("ranking") or [],
            )
    if not pool:
        return PairAssignment()

    min_grid = float(sel.get("min_grid_pnl_usdt", 0.5))
    min_lite = float(sel.get("min_lite_pnl_usdt", 0.05))
    max_grid = int(sel.get("max_grid_pairs", 10))
    max_lite = int(sel.get("max_lite_pairs", 2))
    stake_grid = int(sel.get("probe_stake_grid", profile.get("stake_usdt", 50)))
    stake_grid_safe = int(sel.get("probe_stake_grid_safe", 30))
    stake_lite = int(sel.get("probe_stake_lite", 12))

    probes: list[dict] = []
    pair_scores: dict[str, dict[str, float]] = {p: {} for p in pool}

    grid_ids = ["live_grid", "live_grid_safe"]
    stakes = {"live_grid": stake_grid, "live_grid_safe": stake_grid_safe}
    for pair in pool:
        sym = pair.split("/")[0]
        for sid in grid_ids:
            pnl = probe_pair(root, pair, timerange, sid, stakes[sid])
            pair_scores[pair][sid] = pnl
            probes.append({"pair": pair, "symbol": sym, "strategy": sid, "pnl_usdt": round(pnl, 4)})
        swing_pnl = probe_pair(root, pair, timerange, "lite_swing", stake_lite)
        arb_pnl = probe_pair(root, pair, timerange, "lite_arbitrage", stake_lite)
        pair_scores[pair]["lite_swing"] = swing_pnl
        pair_scores[pair]["lite_arbitrage"] = arb_pnl
        probes.extend(
            [
                {"pair": pair, "symbol": sym, "strategy": "lite_swing", "pnl_usdt": round(swing_pnl, 4)},
                {"pair": pair, "symbol": sym, "strategy": "lite_arbitrage", "pnl_usdt": round(arb_pnl, 4)},
            ]
        )

    assignments: dict[str, list[str]] = {k: [] for k in PROBE_SCENARIOS}
    assigned: set[str] = set()

    for sid in sorted(grid_ids, key=lambda s: PROBE_SCENARIOS[s]["priority"], reverse=True):
        cap = max_grid if sid == "live_grid" else int(sel.get("max_safe_grid_pairs", 5))
        min_pnl = min_grid if sid == "live_grid" else float(sel.get("min_safe_grid_pnl_usdt", 0.2))
        ranked = sorted(
            [(p, pair_scores[p][sid]) for p in pool if p not in assigned and pair_scores[p][sid] >= min_pnl],
            key=lambda x: x[1],
            reverse=True,
        )
        for p, _ in ranked[:cap]:
            assignments[sid].append(p)
            assigned.add(p)

    lite_counts = {"lite_swing": 0, "lite_arbitrage": 0}
    for pair in pool:
        if pair in assigned:
            continue
        swing_pnl = pair_scores[pair]["lite_swing"]
        arb_pnl = pair_scores[pair]["lite_arbitrage"]
        candidates = []
        if swing_pnl >= min_lite and lite_counts["lite_swing"] < max_lite:
            candidates.append(("lite_swing", swing_pnl))
        if arb_pnl >= min_lite and lite_counts["lite_arbitrage"] < max_lite:
            candidates.append(("lite_arbitrage", arb_pnl))
        if not candidates:
            continue
        best_id, _ = max(candidates, key=lambda x: x[1])
        assignments[best_id].append(pair)
        lite_counts[best_id] += 1
        assigned.add(pair)

    union = sorted(assigned)
    ranking = sorted(
        [
            {
                "pair": p,
                "symbol": p.split("/")[0],
                "grid_pnl": round(pair_scores[p].get("live_grid", 0.0), 4),
                "grid_safe_pnl": round(pair_scores[p].get("live_grid_safe", 0.0), 4),
                "swing_pnl": round(pair_scores[p].get("lite_swing", 0.0), 4),
                "arb_pnl": round(pair_scores[p].get("lite_arbitrage", 0.0), 4),
                "assigned_to": next((sid for sid, ps in assignments.items() if p in ps), None),
            }
            for p in pool
        ],
        key=lambda x: max(x["grid_pnl"], x["grid_safe_pnl"], x["swing_pnl"], x["arb_pnl"]),
        reverse=True,
    )
    return PairAssignment(pairs=union, assignments=assignments, probes=probes, ranking=ranking)


def select_pairs(
    root: Path,
    datadir: Path,
    at_ms: int,
    pool: list[str] | None = None,
    profile: dict | None = None,
) -> list[str]:
    return assign_pairs(root, datadir, at_ms, pool, profile).pairs


def pairs_for_scenario(
    root: Path,
    datadir: Path,
    at_ms: int,
    scenario_id: str,
    profile: dict | None = None,
) -> list[str]:
    return assign_pairs(root, datadir, at_ms, profile=profile).assignments.get(scenario_id, [])


def save_assignment(result: PairAssignment, path: Path, at_ms: int, pool: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "at_ms": at_ms,
                "pool": pool or [],
                "pairs": result.pairs,
                "bases": [p.split("/")[0] for p in result.pairs],
                "assignments": {k: v for k, v in result.assignments.items() if v},
                "ranking": result.ranking,
                "probes": result.probes,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def load_assignment(root: Path) -> PairAssignment | None:
    path = root / "simulation/results/selected_player_pairs.json"
    if not path.is_file():
        return None
    data = load_json(path)
    if not data.get("assignments"):
        pairs = data.get("pairs") or []
        return PairAssignment(pairs=pairs, assignments={"live_grid": pairs})
    return PairAssignment(
        pairs=data.get("pairs") or [],
        assignments=data.get("assignments") or {},
        probes=data.get("probes") or [],
        ranking=data.get("ranking") or [],
    )


def _cache_valid(data: dict, at_ms: int, pool: list[str]) -> bool:
    if data.get("at_ms") != at_ms:
        return False
    cached_pool = data.get("pool")
    if cached_pool and set(cached_pool) != set(pool):
        return False
    ranking = data.get("ranking") or []
    if ranking and "swing_pnl" not in ranking[0]:
        return False
    return bool(data.get("assignments"))


def main() -> int:
    manifest = load_json(ROOT / "simulation/config/manifest.json")
    profile = load_json(ROOT / "simulation/config/player_profile.json")
    datadir = ROOT / manifest.get("ctengine_datadir", "simulation/data/ctengine")
    timerange = manifest.get("player_timerange", "20260601-20260625")
    start_s = timerange.split("-")[0]
    at_ms = int(datetime.strptime(start_s, "%Y%m%d").replace(tzinfo=UTC).timestamp() * 1000)
    result = assign_pairs(ROOT, datadir, at_ms, profile=profile)
    out_path = ROOT / "simulation/results/selected_player_pairs.json"
    save_assignment(result, out_path, at_ms, pool_pairs(ROOT))
    print(
        json.dumps(
            {
                "pairs": result.pairs,
                "assignments": {k: v for k, v in result.assignments.items() if v},
                "ranking_top": result.ranking[:5],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

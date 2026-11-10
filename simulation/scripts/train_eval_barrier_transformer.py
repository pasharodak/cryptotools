#!/usr/bin/env python3
"""Train + evaluate 5m barrier Transformer v2 (ATR + setup + L/S + BTC).

Train: 20250101-20260331 (val = last 10% of train by time)
Test:  20260401-20260625
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.ml.barrier_transformer import (  # noqa: E402
    DEFAULT_ATR_PCT_MIN,
    DEFAULT_HORIZON,
    DEFAULT_SL_MULT,
    DEFAULT_TP_MULT,
    DEFAULT_VOL_Z_MIN,
    DEFAULT_WINDOW,
    FEATURE_COLS,
    build_feature_frame,
    make_barrier_sequences,
    predict_proba,
    save_checkpoint,
    threshold_metrics,
    train_barrier_transformer,
)
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402

TRAIN_RANGE = "20250101-20260331"
TEST_RANGE = "20260401-20260625"
BTC_PAIR = "BTC/USDT:USDT"


def _parse_range(tr: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    a, b = tr.split("-")
    return pd.Timestamp(a, tz="UTC"), pd.Timestamp(b, tz="UTC")


def _month_slices(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    out = []
    cur = pd.Timestamp(year=start.year, month=start.month, day=1, tz="UTC")
    while cur < end:
        if cur.month == 12:
            nxt = pd.Timestamp(year=cur.year + 1, month=1, day=1, tz="UTC")
        else:
            nxt = pd.Timestamp(year=cur.year, month=cur.month + 1, day=1, tz="UTC")
        lo, hi = max(cur, start), min(nxt, end)
        if lo < hi:
            out.append((f"{cur.year}-{cur.month:02d}", lo, hi))
        cur = nxt
    return out


def collect_pool(
    pairs: list[str],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    window: int,
    tp_mult: float,
    sl_mult: float,
    horizon: int,
    stride: int,
    limit: int,
    subsample_cap: int | None,
    vol_z_min: float,
    atr_pct_min: float,
    btc_full: pd.DataFrame | None,
) -> dict[str, np.ndarray]:
    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
    xs, ys, rs, ts, sides = [], [], [], [], []
    used = 0
    btc_slice = None
    if btc_full is not None and not btc_full.empty:
        btc_slice = btc_full.loc[(btc_full.index >= start) & (btc_full.index < end)]

    for i, pair in enumerate(pairs, 1):
        if limit and i > limit:
            break
        try:
            df = ds.load(pair, "5m")
        except FileNotFoundError:
            print(f"[{i}] SKIP no data {pair}")
            continue
        df = df.loc[(df.index >= start) & (df.index < end)]
        if len(df) < window + horizon + 50:
            print(f"[{i}] SKIP short {pair} n={len(df)}")
            continue
        feat = build_feature_frame(df, btc_slice)
        X, y, realized, times, side = make_barrier_sequences(
            feat,
            window=window,
            tp_mult=tp_mult,
            sl_mult=sl_mult,
            horizon=horizon,
            stride=stride,
            sides=(1, -1),
            vol_z_min=vol_z_min,
            atr_pct_min=atr_pct_min,
            require_setup=True,
        )
        if len(y) == 0:
            print(f"[{i}] SKIP empty seq {pair}")
            continue
        xs.append(X)
        ys.append(y)
        rs.append(realized)
        ts.append(times)
        sides.append(side)
        used += 1
        print(f"[{i}/{len(pairs)}] {pair.split('/')[0]:12} n={len(y)} pos={float(y.mean()):.3f}")

    if not xs:
        raise SystemExit("empty pool — check data / pairs / timerange")

    X = np.concatenate(xs)
    y = np.concatenate(ys)
    realized = np.concatenate(rs)
    times = np.concatenate(ts)
    side = np.concatenate(sides)
    order = np.argsort(times)
    X, y, realized, times, side = X[order], y[order], realized[order], times[order], side[order]

    if subsample_cap and len(y) > subsample_cap:
        step = max(1, len(y) // subsample_cap)
        X = X[::step][:subsample_cap]
        y = y[::step][:subsample_cap]
        realized = realized[::step][:subsample_cap]
        times = times[::step][:subsample_cap]
        side = side[::step][:subsample_cap]
        print(f"subsampled to n={len(y)}")

    print(f"pool pairs_used={used} n={len(y)} pos_rate={float(y.mean()):.4f}")
    return {"X": X, "y": y, "realized": realized, "times": times, "side": side}


def time_split_train_val(pool: dict[str, np.ndarray], val_frac: float = 0.10):
    n = len(pool["y"])
    n_val = max(1, int(n * val_frac))
    n_tr = n - n_val
    if n_tr < 200:
        raise SystemExit(f"train too small: {n_tr}")
    return {k: v[:n_tr] for k, v in pool.items()}, {k: v[n_tr:] for k, v in pool.items()}


def eval_split(model, mean, std, temperature, pool, *, thr, device, stake, label) -> dict[str, Any]:
    proba = predict_proba(model, pool["X"], mean, std, device=device, temperature=temperature)
    y, realized, times = pool["y"], pool["realized"], pool["times"]
    try:
        from sklearn.metrics import roc_auc_score

        auc = float(roc_auc_score(y, proba)) if len(np.unique(y)) > 1 else None
    except Exception:
        auc = None
    base = threshold_metrics(y, proba, realized, thr=thr, stake=stake)
    gates = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85)
    curve = [threshold_metrics(y, proba, realized, thr=g, stake=stake) for g in gates]
    months = {}
    if len(times):
        for name, lo, hi in _month_slices(
            pd.Timestamp(int(times.min()), unit="ms", tz="UTC"),
            pd.Timestamp(int(times.max()) + 1, unit="ms", tz="UTC"),
        ):
            m = (times >= int(lo.timestamp() * 1000)) & (times < int(hi.timestamp() * 1000))
            if m.any():
                months[name] = threshold_metrics(y[m], proba[m], realized[m], thr=thr, stake=stake)
    return {
        "label": label,
        "n": int(len(y)),
        "pos_rate": round(float(y.mean()), 4),
        "auc": None if auc is None else round(auc, 4),
        "at_thr": base,
        "curve": curve,
        "by_month": months,
    }


def main_barrier(args) -> int:
    train_start, train_end = _parse_range(args.train_range)
    test_start, test_end = _parse_range(args.test_range)
    pairs = pairs_from_source(ROOT, args.pairs, min_start=args.train_range.split("-")[0])

    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
    try:
        btc_full = ds.load(BTC_PAIR, "5m")
    except FileNotFoundError:
        btc_full = None
        print("WARN: no BTC data — context features zeroed")

    print(f"pairs={len(pairs)} train={args.train_range} test={args.test_range} v2 ATR setup L/S")
    train_pool = collect_pool(
        pairs,
        start=train_start,
        end=train_end,
        window=args.window,
        tp_mult=args.tp_mult,
        sl_mult=args.sl_mult,
        horizon=args.horizon,
        stride=args.stride,
        limit=args.limit,
        subsample_cap=args.subsample_cap,
        vol_z_min=args.vol_z_min,
        atr_pct_min=args.atr_pct_min,
        btc_full=btc_full,
    )
    tr, va = time_split_train_val(train_pool, val_frac=0.10)

    result = train_barrier_transformer(
        tr["X"],
        tr["y"],
        va["X"],
        va["y"],
        va["realized"],
        epochs=args.epochs,
        batch=args.batch,
        d_model=args.d_model,
        layers=args.layers,
    )
    thr = float(result.metrics["chosen_thr"])
    print(f"chosen thr={thr} val_auc={result.metrics.get('val_auc')} temp={result.temperature:.3f}")

    import torch

    device = torch.device(result.device)
    test_pool = collect_pool(
        pairs,
        start=test_start,
        end=test_end,
        window=args.window,
        tp_mult=args.tp_mult,
        sl_mult=args.sl_mult,
        horizon=args.horizon,
        stride=args.stride,
        limit=args.limit,
        subsample_cap=None,
        vol_z_min=args.vol_z_min,
        atr_pct_min=args.atr_pct_min,
        btc_full=btc_full,
    )
    test_eval = eval_split(
        result.model, result.mean, result.std, result.temperature, test_pool,
        thr=thr, device=device, stake=args.stake, label="test",
    )
    val_eval = eval_split(
        result.model, result.mean, result.std, result.temperature, va,
        thr=thr, device=device, stake=args.stake, label="val",
    )

    median_atr = float(np.nanmedian(tr["X"][:, -1, FEATURE_COLS.index("atr_pct")])) if len(tr["X"]) else 0.01
    tp_pct = max(0.005, median_atr * args.tp_mult)
    sl_pct = max(0.005, median_atr * args.sl_mult)

    meta = {
        "window": args.window,
        "tp_mult": args.tp_mult,
        "sl_mult": args.sl_mult,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "tp": tp_pct,
        "sl": sl_pct,
        "horizon": args.horizon,
        "stride": args.stride,
        "thr": thr,
        "chosen_thr": thr,
        "vol_z_min": args.vol_z_min,
        "atr_pct_min": args.atr_pct_min,
        "require_setup": True,
        "train_range": args.train_range,
        "test_range": args.test_range,
        "pairs_source": args.pairs,
        "pairs_limit": args.limit,
        "sides": "long_short",
        "feature_cols": FEATURE_COLS,
        "d_model": args.d_model,
        "nhead": 4,
        "layers": args.layers,
        "version": 2,
        "mode": "barrier",
        "created": datetime.now(UTC).isoformat(),
    }
    save_checkpoint(args.model_out, result, meta=meta)

    v1_path = ROOT / "simulation/results/barrier_transformer/report.json"
    v1 = None
    if v1_path.is_file():
        try:
            v1 = json.loads(v1_path.read_text(encoding="utf-8")).get("test_eval", {}).get("at_thr")
        except Exception:
            v1 = None

    report = {
        "created": meta["created"],
        "config": meta,
        "train_metrics": result.metrics,
        "val_eval": val_eval,
        "test_eval": test_eval,
        "model_path": str(Path(args.model_out).as_posix()),
        "vs_v1_test": {"v1": v1, "v2": test_eval["at_thr"]},
        "success_check": {
            "precision_ge_60": bool(
                test_eval["at_thr"]["precision"] is not None and test_eval["at_thr"]["precision"] >= 0.60
            ),
            "coverage_ge_5pct": bool(test_eval["at_thr"]["coverage"] >= 0.05),
            "pnl_positive": bool(test_eval["at_thr"]["pnl_usdt"] > 0),
            "better_pnl_than_v1": bool(
                v1 is not None and test_eval["at_thr"]["pnl_usdt"] > float(v1.get("pnl_usdt") or -1e9)
            ),
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    (ROOT / "simulation/results/barrier_transformer/report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps({"test_at_thr": test_eval["at_thr"], "by_month": test_eval["by_month"], "vs_v1": v1}, indent=2))
    print(f"wrote {out}")
    return 0


GATE_SCENARIOS = [
    "scalp_ema",
    "new_psar",
    "chart3_atrch",
    "scalp_liq_breakout",
    "new_donchian",
    "combo_don_adx_vol",
]
CACHE_DIR = ROOT / "simulation/results/ml_param_experiments/trade_cache"


def _load_gate_rows(scenarios: list[str], split: str) -> list[dict[str, Any]]:
    from simulation.scripts.run_ml_param_experiments import cache_path, load_cached

    rows: list[dict[str, Any]] = []
    for sid in scenarios:
        chunk = load_cached(cache_path(CACHE_DIR, sid, split)) or []
        for r in chunk:
            tr = r.get("trade") or {}
            oms = int(tr.get("open_ms") or r.get("open_ms") or 0)
            if not oms:
                continue
            pnl = float(r.get("profit_abs") if r.get("profit_abs") is not None else tr.get("profit_abs") or 0)
            rows.append(
                {
                    "pair": str(r.get("pair") or ""),
                    "open_ms": oms,
                    "is_short": bool(tr.get("is_short")),
                    "profit_abs": pnl,
                    "scenario_id": sid,
                    "y": 1 if pnl > 0 else 0,
                }
            )
        print(f"cache {sid}/{split}: +{len(chunk)} raw -> pool={len(rows)}")
    return rows


def collect_gate_pool(rows: list[dict[str, Any]], *, window: int, btc_full) -> dict[str, np.ndarray]:
    from simulation.ml.barrier_transformer import sequences_at_open_ms

    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
    by_pair: dict[str, list[dict]] = {}
    for r in rows:
        by_pair.setdefault(r["pair"], []).append(r)

    xs, ys, rs, ts = [], [], [], []
    for i, (pair, grp) in enumerate(by_pair.items(), 1):
        try:
            df = ds.load(pair, "5m")
        except FileNotFoundError:
            continue
        btc_slice = None
        if btc_full is not None and not btc_full.empty:
            btc_slice = btc_full.reindex(df.index, method="ffill")
            # keep as OHLCV-like for build_feature_frame — use original btc aligned
            btc_slice = btc_full.loc[
                (btc_full.index >= df.index.min() - pd.Timedelta(days=2))
                & (btc_full.index <= df.index.max() + pd.Timedelta(days=1))
            ]
        feat = build_feature_frame(df, btc_slice if btc_full is not None else None)
        oms = [g["open_ms"] for g in grp]
        sides = [-1 if g["is_short"] else 1 for g in grp]
        X = sequences_at_open_ms(feat, oms, sides, window=window)
        y = np.asarray([g["y"] for g in grp], dtype=np.int64)
        # realized as profit_abs / stake proxy → use profit/15 as return-ish for pnl metric
        realized = np.asarray([g["profit_abs"] / 15.0 for g in grp], dtype=np.float32)
        times = np.asarray(oms, dtype=np.int64)
        xs.append(X)
        ys.append(y)
        rs.append(realized)
        ts.append(times)
        if i % 25 == 0:
            print(f"gate feats [{i}/{len(by_pair)}] {pair.split('/')[0]}")

    if not xs:
        raise SystemExit("empty gate pool")
    X = np.concatenate(xs)
    y = np.concatenate(ys)
    realized = np.concatenate(rs)
    times = np.concatenate(ts)
    order = np.argsort(times)
    print(f"gate pool n={len(y)} pos_rate={float(y.mean()):.4f}")
    return {
        "X": X[order],
        "y": y[order],
        "realized": realized[order],
        "times": times[order],
    }


def main_gate(args) -> int:
    scenarios = [s.strip() for s in args.gate_scenarios.split(",") if s.strip()]
    train_rows = _load_gate_rows(scenarios, "train")
    test_rows = _load_gate_rows(scenarios, "test")
    if not train_rows or not test_rows:
        raise SystemExit("missing trade cache for gate mode")

    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
    try:
        btc_full = ds.load(BTC_PAIR, "5m")
    except FileNotFoundError:
        btc_full = None

    print(f"gate mode scenarios={scenarios} train_rows={len(train_rows)} test_rows={len(test_rows)}")
    train_pool = collect_gate_pool(train_rows, window=args.window, btc_full=btc_full)
    # optional subsample
    if args.subsample_cap and len(train_pool["y"]) > args.subsample_cap:
        step = max(1, len(train_pool["y"]) // args.subsample_cap)
        train_pool = {k: v[::step][: args.subsample_cap] for k, v in train_pool.items()}
        print(f"subsampled train to n={len(train_pool['y'])}")

    tr, va = time_split_train_val(train_pool, val_frac=0.10)
    result = train_barrier_transformer(
        tr["X"],
        tr["y"],
        va["X"],
        va["y"],
        va["realized"],
        epochs=args.epochs,
        batch=args.batch,
        d_model=args.d_model,
        nhead=args.nhead,
        layers=args.layers,
        lr=args.lr,
    )
    thr = float(result.metrics["chosen_thr"])
    print(f"chosen thr={thr} val_auc={result.metrics.get('val_auc')}")

    import torch

    device = torch.device(result.device)
    test_pool = collect_gate_pool(test_rows, window=args.window, btc_full=btc_full)
    test_eval = eval_split(
        result.model, result.mean, result.std, result.temperature, test_pool,
        thr=thr, device=device, stake=args.stake, label="test_gate",
    )
    val_eval = eval_split(
        result.model, result.mean, result.std, result.temperature, va,
        thr=thr, device=device, stake=args.stake, label="val_gate",
    )

    meta = {
        "window": args.window,
        "tp_pct": 0.008,
        "sl_pct": 0.01,
        "tp": 0.008,
        "sl": 0.01,
        "thr": thr,
        "chosen_thr": thr,
        "require_setup": False,
        "vol_z_min": 0.0,
        "atr_pct_min": 0.0,
        "train_range": args.train_range,
        "test_range": args.test_range,
        "gate_scenarios": scenarios,
        "feature_cols": FEATURE_COLS,
        "d_model": args.d_model,
        "nhead": args.nhead,
        "layers": args.layers,
        "dim_ff": 4 * args.d_model,
        "version": 4,
        "mode": "strategy_gate",
        "device": result.device,
        "created": datetime.now(UTC).isoformat(),
    }
    # keep previous small model
    out_model = Path(args.model_out)
    if out_model.is_file():
        bak = out_model.with_name(out_model.stem + "_prev.pt")
        try:
            out_model.replace(bak)
            print(f"backed up previous weights -> {bak}")
        except Exception as exc:
            print(f"backup skip: {exc}")
    save_checkpoint(args.model_out, result, meta=meta)

    v1 = None
    v1_path = ROOT / "simulation/results/barrier_transformer/report.json"
    if v1_path.is_file():
        try:
            prev = json.loads(v1_path.read_text(encoding="utf-8"))
            v1 = (prev.get("test_eval") or {}).get("at_thr")
        except Exception:
            pass

    report = {
        "created": meta["created"],
        "config": meta,
        "train_metrics": result.metrics,
        "val_eval": val_eval,
        "test_eval": test_eval,
        "model_path": str(Path(args.model_out).as_posix()),
        "vs_prior": {"prior": v1, "gate": test_eval["at_thr"]},
        "success_check": {
            "precision_ge_60": bool(
                test_eval["at_thr"]["precision"] is not None and test_eval["at_thr"]["precision"] >= 0.60
            ),
            "pnl_positive": bool(test_eval["at_thr"]["pnl_usdt"] > 0),
            "auc_ge_55": bool((test_eval.get("auc") or 0) >= 0.55),
        },
    }
    out = Path(args.out)
    if out.name == "report_v2.json":
        out = out.with_name("report_gate.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    (ROOT / "simulation/results/barrier_transformer/report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps({"test_at_thr": test_eval["at_thr"], "by_month": test_eval["by_month"], "auc": test_eval.get("auc")}, indent=2))
    print(f"wrote {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("barrier", "gate"), default="gate")
    ap.add_argument("--pairs", default="prod200")
    ap.add_argument("--train-range", default=TRAIN_RANGE)
    ap.add_argument("--test-range", default=TEST_RANGE)
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--tp-mult", type=float, default=DEFAULT_TP_MULT)
    ap.add_argument("--sl-mult", type=float, default=DEFAULT_SL_MULT)
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    ap.add_argument("--stride", type=int, default=6)
    ap.add_argument("--vol-z-min", type=float, default=DEFAULT_VOL_Z_MIN)
    ap.add_argument("--atr-pct-min", type=float, default=DEFAULT_ATR_PCT_MIN)
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--d-model", type=int, default=192)
    ap.add_argument("--nhead", type=int, default=8)
    ap.add_argument("--layers", type=int, default=4)
    ap.add_argument("--lr", type=float, default=6e-4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--subsample-cap", type=int, default=80_000)
    ap.add_argument("--stake", type=float, default=15.0)
    ap.add_argument("--gate-scenarios", default=",".join(GATE_SCENARIOS))
    ap.add_argument("--out", default=str(ROOT / "simulation/results/barrier_transformer/report_gate_gpu.json"))
    ap.add_argument(
        "--model-out",
        default=str(ROOT / "simulation/data/models/barrier_transformer_5m_gpu.pt"),
    )
    args = ap.parse_args()
    if args.mode == "gate":
        return main_gate(args)
    return main_barrier(args)


if __name__ == "__main__":
    raise SystemExit(main())

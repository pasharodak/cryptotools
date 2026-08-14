#!/usr/bin/env python3
"""TS-family models for 3 prod scenarios (April cut).

Baselines: ETS / AR(1) logistic
GBDT: LightGBM on wide + lag/volume/GARCH-family features
Seq: LSTM + tiny Transformer (attention)
Vol: GARCH-family features used in GBDT; standalone vol-z gate probe

Scenarios: new_psar, chart3_atrch, trend_breakout
Train 20250101-20260331 / Test 20260401-20260625
"""
from __future__ import annotations

import json
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from simulation.ml.entry_seq_model import train_seq_classifier
from simulation.ml.market_features import MarketFeatureStore, manifest_datadir
from simulation.ml.pnl_classifier import activate_feature_set, build_dataframe, feature_columns
from simulation.ml.ts_entry_features import (
    SEQ_FEATURE_COLS,
    TS_RICH_FEATURES,
    compute_ts_feature_frame,
    sequences_at_times,
)
from simulation.scripts.run_legacy_april_cut_ml import load_scenario_trades, split_trades
from simulation.scripts.run_ml_param_experiments import (
    cache_path,
    load_cached,
    timerange_to_ms,
)
from simulation.scripts.run_scalp_strategies_compare import summarize

TRAIN_RANGE = "20250101-20260331"
TEST_RANGE = "20260401-20260625"
CACHE_DIR = ROOT / "simulation/results/ml_param_experiments/trade_cache"
OUT_DIR = ROOT / "simulation/results/ts_family_3scen_ml"
GATES = (0.45, 0.50, 0.55, 0.60, 0.65, 0.70)
SEQ_WINDOW = 32

SCENARIOS = [
    {"scenario_id": "new_psar", "label": "#1 Parabolic SAR flip", "source": "cache"},
    {"scenario_id": "chart3_atrch", "label": "#2 ATR channel breakout", "source": "cache"},
    {"scenario_id": "trend_breakout", "label": "#32 Breakout-Retest", "source": "trade_db"},
]

ETS_FEATURES = ["ets_level_dev", "ets_trend_12_48", "ets_forecast_ret", "ets_abs_resid"]
ARIMA_FEATURES = ["ar1_rho_96", "ar1_resid", "ar1_forecast_ret", "ret_lag_1", "ret_lag_2"]
GARCH_FEATURES = ["garch_ewma_vol", "garch_ewma_vol_z", "garch_arch_vol", "garch_arch_vol_z"]


class TsFeatureStore:
    """Cached TS feature frames per pair."""

    def __init__(self, datadir: Path, exchange: str = "bybit"):
        self.mkt = MarketFeatureStore(datadir, exchange=exchange)
        self._ts: dict[str, pd.DataFrame | None] = {}

    def ts_frame(self, pair: str, timeframe: str = "5m") -> pd.DataFrame | None:
        key = f"{pair}|{timeframe}"
        if key in self._ts:
            return self._ts[key]
        try:
            ohlcv = self.mkt.ds.load(pair, timeframe)
        except FileNotFoundError:
            self._ts[key] = None
            return None
        frame = compute_ts_feature_frame(ohlcv)
        self._ts[key] = frame
        return frame

    def lookup_row(self, pair: str, open_ms: int, cols: list[str], timeframe: str = "5m") -> dict[str, float]:
        empty = {c: float("nan") for c in cols}
        fr = self.ts_frame(pair, timeframe)
        if fr is None or fr.empty or not open_ms:
            return empty
        ts = pd.Timestamp(int(open_ms), unit="ms", tz="UTC")
        idx = fr.index
        if getattr(idx, "tz", None) is None:
            # reindex compare
            pass
        pos = fr.index.get_indexer([ts], method="pad")
        if pos[0] < 0:
            return empty
        row = fr.iloc[pos[0]]
        out: dict[str, float] = {}
        for c in cols:
            v = row.get(c) if c in fr.columns else np.nan
            out[c] = float(v) if v is not None and not pd.isna(v) else float("nan")
        return out


def load_trades(sid: str, source: str) -> tuple[list[dict], list[dict]]:
    cut_ms = timerange_to_ms(TEST_RANGE)[0]
    end_ms = timerange_to_ms(TEST_RANGE)[1]
    if source == "cache":
        train = load_cached(cache_path(CACHE_DIR, sid, "train")) or []
        test = load_cached(cache_path(CACHE_DIR, sid, "test")) or []
        if not train or not test:
            raise FileNotFoundError(f"missing cache for {sid}")
        return train, test
    rows = load_scenario_trades(sid)
    train, test = split_trades(rows, cut_ms)
    test = [r for r in test if int((r.get("trade") or {}).get("open_ms") or 0) <= end_ms]
    return train, test


def enrich_ts(df: pd.DataFrame, store: TsFeatureStore, cols: list[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        out[c] = np.nan
    if out.empty:
        return out
    for pair, grp in out.groupby(out["pair"].astype(str), sort=False):
        fr = store.ts_frame(str(pair))
        if fr is None or fr.empty:
            continue
        idx = fr.index
        if getattr(idx, "tz", None) is None:
            try:
                idx = idx.tz_localize("UTC")
                fr = fr.copy()
                fr.index = idx
            except Exception:
                pass
        oms = grp["open_ms"].astype("int64").tolist()
        ts = pd.to_datetime(oms, unit="ms", utc=True)
        pos = fr.index.get_indexer(ts, method="pad")
        for col in cols:
            if col not in fr.columns:
                continue
            vals = fr[col].to_numpy()
            col_out = np.full(len(pos), np.nan, dtype=float)
            ok = pos >= 0
            col_out[ok] = vals[pos[ok]]
            out.loc[grp.index, col] = col_out
    return out


def build_seq_matrix(df: pd.DataFrame, store: TsFeatureStore) -> np.ndarray:
    n = len(df)
    out = np.zeros((n, SEQ_WINDOW, len(SEQ_FEATURE_COLS)), dtype=np.float32)
    if n == 0:
        return out
    # preserve row order
    order_idx = list(df.index)
    pos_map = {idx: i for i, idx in enumerate(order_idx)}
    for pair, grp in df.groupby(df["pair"].astype(str), sort=False):
        fr = store.ts_frame(str(pair))
        oms = grp["open_ms"].astype("int64").tolist()
        arr = sequences_at_times(
            fr if fr is not None else pd.DataFrame(),
            oms,
            window=SEQ_WINDOW,
        )
        for local_i, idx in enumerate(grp.index):
            out[pos_map[idx]] = arr[local_i]
    return out


def profit_by_id(trades: list[dict]) -> dict[Any, float]:
    out = {}
    for t in trades:
        rid = t.get("id")
        pnl = t.get("profit_abs")
        if pnl is None:
            pnl = (t.get("trade") or {}).get("profit_abs")
        if rid is not None:
            out[rid] = float(pnl or 0)
    return out


def gate_pnl(proba: np.ndarray, pnl: np.ndarray, thr: float) -> dict[str, Any]:
    kept = (proba >= thr) & (proba >= 0.5)
    n = int(kept.sum())
    if n == 0:
        return {"min_profit_proba": thr, "n": 0, "pnl": 0.0, "winrate": 0.0, "keep_rate": 0.0}
    p = pnl[kept]
    wins = int((p >= 0).sum())
    return {
        "min_profit_proba": thr,
        "n": n,
        "pnl": round(float(p.sum()), 4),
        "winrate": round(wins / n, 4),
        "keep_rate": round(n / len(pnl), 4),
    }


def eval_proba(name: str, proba: np.ndarray, y: np.ndarray, pnl: np.ndarray, thr: float) -> dict[str, Any]:
    pred = (proba >= 0.5).astype(int)
    auc = None
    try:
        if len(np.unique(y)) > 1:
            auc = float(roc_auc_score(y, proba))
    except Exception:
        pass
    fixed = gate_pnl(proba, pnl, thr)
    sweep = [gate_pnl(proba, pnl, g) for g in GATES]
    best = max(sweep, key=lambda x: x["pnl"])
    return {
        "id": name,
        "classifier": {
            "accuracy": round(float(accuracy_score(y, pred)), 4),
            "roc_auc": None if auc is None else round(auc, 4),
        },
        "score": fixed["pnl"],
        "gate": fixed,
        "gate_sweep": sweep,
        "best_gate": best,
    }


def fit_logistic(train_x: pd.DataFrame, y: np.ndarray, cols: list[str]) -> Pipeline:
    pipe = Pipeline(
        [
            ("imp", SimpleImputer(strategy="median")),
            ("sc", StandardScaler()),
            ("clf", LogisticRegression(max_iter=400, class_weight="balanced")),
        ]
    )
    pipe.fit(train_x[cols], y)
    return pipe


def fit_lgbm(train_x: pd.DataFrame, y: np.ndarray, cols: list[str]) -> Pipeline:
    base = LGBMClassifier(
        n_estimators=400,
        learning_rate=0.05,
        max_depth=8,
        num_leaves=63,
        min_child_samples=25,
        subsample=0.8,
        colsample_bytree=0.8,
        class_weight="balanced",
        random_state=42,
        verbose=-1,
        n_jobs=-1,
    )
    pipe = Pipeline(
        [
            ("imp", SimpleImputer(strategy="median")),
            ("clf", CalibratedClassifierCV(base, cv=3, method="sigmoid")),
        ]
    )
    pipe.fit(train_x[cols], y)
    return pipe


def predict_profit_proba(pipe: Pipeline, x: pd.DataFrame, cols: list[str]) -> np.ndarray:
    proba = pipe.predict_proba(x[cols])
    clf = pipe.named_steps["clf"]
    classes = list(getattr(clf, "classes_", [0, 1]))
    pidx = classes.index(1) if 1 in classes else 1
    return proba[:, pidx]


def run_scenario(sc: dict[str, str], store: TsFeatureStore, cut_ms: int) -> dict[str, Any]:
    sid = sc["scenario_id"]
    label = sc["label"]
    print(f"\n======== {label} ({sid}) ========", flush=True)

    activate_feature_set("wide")
    train_tr, test_tr = load_trades(sid, sc["source"])
    print(f"trades train={len(train_tr)} test={len(test_tr)}", flush=True)

    # Market wide features via existing builder
    mkt_store = store.mkt
    mkt_store._frames.clear()
    train_df = build_dataframe(train_tr, mkt_store)
    test_df = build_dataframe(test_tr, mkt_store)
    train_df = train_df[train_df["open_ms"] < cut_ms].copy()
    test_df = test_df[test_df["open_ms"] >= cut_ms].copy()
    print(f"frames train={len(train_df)} test={len(test_df)} — enriching TS…", flush=True)

    # Pre-warm TS frames for unique pairs
    pairs = sorted(set(train_df["pair"].astype(str)) | set(test_df["pair"].astype(str)))
    for i, p in enumerate(pairs, 1):
        store.ts_frame(p)
        if i % 40 == 0 or i == len(pairs):
            print(f"  ts frames {i}/{len(pairs)}", flush=True)

    train_df = enrich_ts(train_df, store, TS_RICH_FEATURES)
    test_df = enrich_ts(test_df, store, TS_RICH_FEATURES)

    y_tr = train_df["label"].to_numpy()
    y_te = test_df["label"].to_numpy()
    pmap = profit_by_id(train_tr + test_tr)
    pnl_te = np.array([pmap.get(i, 0.0) for i in test_df["id"].tolist()], dtype=float)
    raw = summarize(test_tr)

    base_cols = [c for c in feature_columns() if c in train_df.columns and c != "label"]
    # numeric-only for logistic baselines / lgbm (drop high-card cat for logistic)
    num_base = [c for c in base_cols if c not in (
        "scenario_id", "pair_base", "scan_type", "group", "strategy", "is_short"
    )]
    rich_cols = num_base + [c for c in TS_RICH_FEATURES if c in train_df.columns]

    results: list[dict[str, Any]] = []

    # 1) ETS baseline
    print("  -> baseline_ets_logistic", flush=True)
    pipe = fit_logistic(train_df, y_tr, ETS_FEATURES)
    proba = predict_profit_proba(pipe, test_df, ETS_FEATURES)
    results.append(eval_proba("baseline_ets_logistic", proba, y_te, pnl_te, 0.55))

    # 2) ARIMA-style AR(1) baseline
    print("  -> baseline_ar1_logistic", flush=True)
    pipe = fit_logistic(train_df, y_tr, ARIMA_FEATURES)
    proba = predict_profit_proba(pipe, test_df, ARIMA_FEATURES)
    results.append(eval_proba("baseline_ar1_logistic", proba, y_te, pnl_te, 0.55))

    # 3) GARCH features only (risk/vol probe as classifier)
    print("  -> baseline_garch_logistic", flush=True)
    pipe = fit_logistic(train_df, y_tr, GARCH_FEATURES)
    proba = predict_profit_proba(pipe, test_df, GARCH_FEATURES)
    results.append(eval_proba("baseline_garch_logistic", proba, y_te, pnl_te, 0.55))

    # 4) GBDT rich (wide TA + lags + vol + ETS/AR/GARCH)
    print("  -> lgbm_ts_rich_gate045", flush=True)
    pipe = fit_lgbm(train_df, y_tr, rich_cols)
    proba = predict_profit_proba(pipe, test_df, rich_cols)
    row = eval_proba("lgbm_ts_rich_gate045", proba, y_te, pnl_te, 0.45)
    row["n_features"] = len(rich_cols)
    results.append(row)

    print("  -> lgbm_ts_rich_gate070", flush=True)
    row = eval_proba("lgbm_ts_rich_gate070", proba, y_te, pnl_te, 0.70)
    row["n_features"] = len(rich_cols)
    results.append(row)

    # 5) Sequences: LSTM + Transformer
    print("  -> building sequences…", flush=True)
    X_tr = build_seq_matrix(train_df, store)
    X_te = build_seq_matrix(test_df, store)
    print(f"  seq shapes train={X_tr.shape} test={X_te.shape}", flush=True)

    for kind in ("lstm", "transformer"):
        print(f"  -> seq_{kind}", flush=True)
        try:
            tr = train_seq_classifier(X_tr, y_tr, X_te, y_te, kind=kind, epochs=8, batch=256)
            thr = 0.55 if sid != "trend_breakout" else 0.70
            row = eval_proba(f"seq_{kind}", tr.proba_test, y_te, pnl_te, thr)
            row["classifier"] = {**row["classifier"], **tr.metrics}
            results.append(row)
        except Exception as exc:
            results.append({"id": f"seq_{kind}", "error": str(exc), "score": None})
            print(f"     ERROR {kind}: {exc}", flush=True)

    ok = [r for r in results if r.get("score") is not None]
    winner = max(ok, key=lambda r: float(r["score"])) if ok else None
    best_sweep = (
        max(ok, key=lambda r: float((r.get("best_gate") or {}).get("pnl") or -1e18)) if ok else None
    )

    out = {
        "scenario_id": sid,
        "label": label,
        "n_train": len(train_tr),
        "n_test": len(test_tr),
        "n_train_rows": len(train_df),
        "n_test_rows": len(test_df),
        "test_raw": raw,
        "experiments": results,
        "winner_fixed": (winner or {}).get("id"),
        "winner_fixed_pnl": (winner or {}).get("score"),
        "winner_sweep": (best_sweep or {}).get("id"),
        "winner_sweep_pnl": ((best_sweep or {}).get("best_gate") or {}).get("pnl"),
        "winner_sweep_thr": ((best_sweep or {}).get("best_gate") or {}).get("min_profit_proba"),
    }
    for r in results:
        if r.get("score") is not None:
            print(
                f"     {r['id']}: auc={r['classifier'].get('roc_auc')} "
                f"pnl={r['score']} best={r['best_gate']['pnl']}@{r['best_gate']['min_profit_proba']}",
                flush=True,
            )
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cut_ms = timerange_to_ms(TEST_RANGE)[0]
    datadir = manifest_datadir(ROOT)
    store = TsFeatureStore(datadir)

    rows = []
    for sc in SCENARIOS:
        row = run_scenario(sc, store, cut_ms)
        rows.append(row)
        (OUT_DIR / f"{sc['scenario_id']}.json").write_text(
            json.dumps(row, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "train_range": TRAIN_RANGE,
        "test_range": TEST_RANGE,
        "methods": [
            "baseline_ets_logistic",
            "baseline_ar1_logistic",
            "baseline_garch_logistic",
            "lgbm_ts_rich",
            "seq_lstm",
            "seq_transformer",
        ],
        "scenarios": rows,
        "ranking": [
            {
                "scenario_id": r["scenario_id"],
                "label": r["label"],
                "winner_fixed": r.get("winner_fixed"),
                "pnl_fixed": r.get("winner_fixed_pnl"),
                "winner_sweep": r.get("winner_sweep"),
                "pnl_sweep": r.get("winner_sweep_pnl"),
            }
            for r in rows
        ],
    }
    (OUT_DIR / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print("\n========== RANKING ==========", flush=True)
    for r in report["ranking"]:
        line = (
            f"{r['label']}: fixed={r['pnl_fixed']} ({r['winner_fixed']})  "
            f"sweep={r['pnl_sweep']} ({r['winner_sweep']})"
        )
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    print(f"\nReport: {OUT_DIR / 'report.json'}", flush=True)
    activate_feature_set("core")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

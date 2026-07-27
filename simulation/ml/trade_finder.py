"""ML trade finder — scan OHLCV bars for profitable long/short entries (market-only features)."""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from xgboost import XGBClassifier

from simulation.exchange_sim.datastore import HistoricalDatastore
from simulation.ml.market_features import (
    MARKET_FEATURES,
    compute_indicator_frame,
    manifest_datadir,
    min_indicator_warmup,
)
from simulation.ml.pnl_classifier import _FramePreprocess, load_trades, pair_base

MODEL_DIR = "simulation/results/trade_db/models"
MODEL_FILE = "trade_finder.joblib"
META_FILE = "trade_finder_meta.json"
CONFIG_FILE = "simulation/config/trade_finder.json"

CAT_FEATURES = ["pair_base", "is_short"]
NUM_FEATURES = ["hour_utc", "dow_utc"] + MARKET_FEATURES


def feature_columns() -> list[str]:
    return CAT_FEATURES + NUM_FEATURES


def load_config(root: Path, config_rel: str | None = None) -> dict[str, Any]:
    path = root / (config_rel or CONFIG_FILE)
    defaults = {
        "timeframe": "5m",
        "tp_atr_mult": 1.5,
        "sl_atr_mult": 1.0,
        "max_bars": 36,
        "sample_stride": 12,
        "scan_stride": 4,
        "min_confidence": 0.55,
        "stake_usdt": 10.0,
        "cooldown_bars": 12,
        "train_timerange": "20251101-20260531",
        "model": "xgboost",
        "train_pairs_source": "player_pair_pool",
    }
    if not path.is_file():
        return defaults
    return {**defaults, **json.loads(path.read_text(encoding="utf-8"))}


def train_pairs(root: Path, cfg: dict[str, Any]) -> list[str]:
    source = cfg.get("train_pairs_source", "player_pair_pool")
    if source == "extension_pairs":
        path = root / "simulation/config/extension_pairs.json"
    elif source == "prod200":
        path = root / "simulation/config/prod_pairs_200.json"
    else:
        path = root / "simulation/config/player_pair_pool.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("pairs") or data.get("whitelist") or []


def parse_timerange_ms(timerange: str) -> tuple[int, int]:
    a, b = timerange.split("-")
    start = int(datetime.strptime(a, "%Y%m%d").replace(tzinfo=UTC).timestamp() * 1000)
    end = int(
        datetime.strptime(b, "%Y%m%d").replace(hour=23, minute=59, second=59, tzinfo=UTC).timestamp() * 1000
    )
    return start, end


def triple_barrier_label(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    atr_pct: np.ndarray,
    idx: int,
    *,
    is_short: bool,
    tp_mult: float,
    sl_mult: float,
    max_bars: int,
) -> int:
    entry = float(close[idx])
    ap = float(atr_pct[idx])
    if entry <= 0 or np.isnan(ap) or ap <= 0:
        return 0
    atr = ap * entry
    if is_short:
        tp_price = entry - tp_mult * atr
        sl_price = entry + sl_mult * atr
    else:
        tp_price = entry + tp_mult * atr
        sl_price = entry - sl_mult * atr

    end = min(idx + max_bars, len(close) - 1)
    for j in range(idx + 1, end + 1):
        if is_short:
            if low[j] <= tp_price:
                return 1
            if high[j] >= sl_price:
                return 0
        else:
            if high[j] >= tp_price:
                return 1
            if low[j] <= sl_price:
                return 0
    return 0


def simulate_trade_pnl(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    atr_pct: np.ndarray,
    idx: int,
    *,
    is_short: bool,
    stake: float,
    tp_mult: float,
    sl_mult: float,
    max_bars: int,
) -> tuple[float, int, str]:
    """Return (pnl_usdt, exit_bar_offset, exit_reason)."""
    entry = float(close[idx])
    ap = float(atr_pct[idx])
    if entry <= 0 or np.isnan(ap) or ap <= 0:
        return 0.0, 0, "invalid"
    atr = ap * entry
    if is_short:
        tp_price = entry - tp_mult * atr
        sl_price = entry + sl_mult * atr
    else:
        tp_price = entry + tp_mult * atr
        sl_price = entry - sl_mult * atr

    end = min(idx + max_bars, len(close) - 1)
    for j in range(idx + 1, end + 1):
        if is_short:
            if low[j] <= tp_price:
                pct = (entry - tp_price) / entry
                return round(stake * pct, 4), j - idx, "tp"
            if high[j] >= sl_price:
                pct = (entry - sl_price) / entry
                return round(stake * pct, 4), j - idx, "sl"
        else:
            if high[j] >= tp_price:
                pct = (tp_price - entry) / entry
                return round(stake * pct, 4), j - idx, "tp"
            if low[j] <= sl_price:
                pct = (sl_price - entry) / entry
                return round(stake * pct, 4), j - idx, "sl"
    final = float(close[end])
    if is_short:
        pct = (entry - final) / entry
    else:
        pct = (final - entry) / entry
    return round(stake * pct, 4), end - idx, "timeout"


def build_pair_samples(
    pair: str,
    ohlcv: pd.DataFrame,
    ind: pd.DataFrame,
    cfg: dict[str, Any],
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    stride = int(cfg.get("sample_stride") or 12)
    max_bars = int(cfg.get("max_bars") or 36)
    tp_mult = float(cfg.get("tp_atr_mult") or 1.5)
    sl_mult = float(cfg.get("sl_atr_mult") or 1.0)
    tf = cfg.get("timeframe", "5m")
    warmup = min_indicator_warmup(tf)

    merged = ohlcv.join(ind, how="inner")
    if merged.empty:
        return []

    if start_ms is not None:
        merged = merged[merged.index >= pd.Timestamp(start_ms, unit="ms", tz="UTC")]
    if end_ms is not None:
        merged = merged[merged.index <= pd.Timestamp(end_ms, unit="ms", tz="UTC")]
    if len(merged) <= warmup + max_bars + 2:
        return []

    high = merged["high"].to_numpy(dtype=float)
    low = merged["low"].to_numpy(dtype=float)
    close = merged["close"].to_numpy(dtype=float)
    atr_pct = merged["atr_pct_14"].to_numpy(dtype=float)
    rows: list[dict[str, Any]] = []

    for i in range(warmup, len(merged) - max_bars - 1, stride):
        if np.isnan(atr_pct[i]) or atr_pct[i] <= 0:
            continue
        trend_12h = merged.iloc[i].get("trend_12h")
        if trend_12h is None or pd.isna(trend_12h):
            continue
        ts = merged.index[i]
        hour = ts.hour
        dow = ts.weekday()
        open_ms = int(ts.timestamp() * 1000)
        base_feats = {
            "pair": pair,
            "pair_base": pair_base(pair),
            "open_ms": open_ms,
            "hour_utc": hour,
            "dow_utc": dow,
        }
        for col in MARKET_FEATURES:
            val = merged.iloc[i].get(col)
            base_feats[col] = float(val) if val is not None and not pd.isna(val) else float("nan")

        for is_short in (False, True):
            label = triple_barrier_label(
                high, low, close, atr_pct, i,
                is_short=is_short, tp_mult=tp_mult, sl_mult=sl_mult, max_bars=max_bars,
            )
            rows.append({**base_feats, "is_short": "1" if is_short else "0", "label": label})
    return rows


def build_dataset_from_trade_db(root: Path, pairs: list[str], cfg: dict[str, Any]) -> pd.DataFrame:
    """Labels from real trade_db outcomes; market-only features."""
    datadir = manifest_datadir(root)
    ds = HistoricalDatastore(datadir)
    tf = cfg.get("timeframe", "5m")
    trades = [t for t in load_trades(root / "simulation" / "results" / "trade_db") if (t.get("pair") in pairs)]
    rows: list[dict[str, Any]] = []
    for rec in trades:
        tr = rec.get("trade") or {}
        open_ms = int(tr.get("open_ms") or 0)
        pair = rec.get("pair") or ""
        if not pair or not open_ms:
            continue
        try:
            ohlcv = ds.load(pair, tf)
            ind = compute_indicator_frame(ohlcv, timeframe=tf)
        except FileNotFoundError:
            continue
        merged = ohlcv.join(ind, how="inner")
        ts = pd.Timestamp(open_ms, unit="ms", tz="UTC")
        pos = merged.index.get_indexer([ts], method="pad")
        if pos[0] < 0:
            continue
        i = pos[0]
        dt = ts.to_pydatetime()
        row: dict[str, Any] = {
            "pair": pair,
            "pair_base": pair_base(pair),
            "open_ms": open_ms,
            "is_short": "1" if tr.get("is_short") else "0",
            "hour_utc": dt.hour,
            "dow_utc": dt.weekday(),
            "label": 1 if float(rec.get("profit_abs") or 0) >= 0 else 0,
            "source": "trade_db",
        }
        for col in MARKET_FEATURES:
            val = merged.iloc[i].get(col)
            row[col] = float(val) if val is not None and not pd.isna(val) else float("nan")
        rows.append(row)

    ratio = float(cfg.get("random_negative_ratio") or 0.5)
    n_random = int(len(rows) * ratio)
    if n_random > 0:
        start_ms, end_ms = parse_timerange_ms(cfg["train_timerange"])
        stride = int(cfg.get("sample_stride") or 12) * 2
        per_pair = max(50, n_random // max(len(pairs), 1))
        for pair in pairs:
            try:
                ohlcv = ds.load(pair, tf)
                ind = compute_indicator_frame(ohlcv, timeframe=tf)
            except FileNotFoundError:
                continue
            merged = ohlcv.join(ind, how="inner")
            merged = merged[(merged.index >= pd.Timestamp(start_ms, unit="ms", tz="UTC"))]
            merged = merged[(merged.index <= pd.Timestamp(end_ms, unit="ms", tz="UTC"))]
            if len(merged) < 100:
                continue
            used_ms = {r["open_ms"] for r in rows if r["pair"] == pair}
            added = 0
            warmup = min_indicator_warmup(tf)
            for i in range(warmup, len(merged) - 50, stride):
                ts = merged.index[i]
                open_ms = int(ts.timestamp() * 1000)
                if open_ms in used_ms:
                    continue
                row = {
                    "pair": pair,
                    "pair_base": pair_base(pair),
                    "open_ms": open_ms,
                    "is_short": "0",
                    "hour_utc": ts.hour,
                    "dow_utc": ts.weekday(),
                    "label": 0,
                    "source": "random_bar",
                }
                for col in MARKET_FEATURES:
                    val = merged.iloc[i].get(col)
                    row[col] = float(val) if val is not None and not pd.isna(val) else float("nan")
                rows.append(row)
                added += 1
                if added >= per_pair:
                    break
    print(f"  trade_db: {sum(1 for r in rows if r.get('source')=='trade_db')} "
          f"(profit {sum(1 for r in rows if r.get('source')=='trade_db' and r['label']==1)})")
    print(f"  random negatives: {sum(1 for r in rows if r.get('source')=='random_bar')}")
    return pd.DataFrame(rows)


def _finder_pool_init(root_str: str) -> None:
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def _collect_pair_samples_task(task: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str | None]:
    """Process-pool worker: build triple-barrier samples for one pair."""
    _finder_pool_init(task["root"])
    pair = task["pair"]
    cfg = task["cfg"]
    try:
        ds = HistoricalDatastore(Path(task["datadir"]))
        tf = cfg.get("timeframe", "5m")
        ohlcv = ds.load(pair, tf)
        ind = compute_indicator_frame(ohlcv, timeframe=tf)
        samples = build_pair_samples(
            pair,
            ohlcv,
            ind,
            cfg,
            start_ms=task["start_ms"],
            end_ms=task["end_ms"],
        )
        return pair, samples, None
    except FileNotFoundError:
        return pair, [], "no OHLCV"
    except Exception as exc:
        return pair, [], str(exc)


_MP_BT_PIPE: Pipeline | None = None
_MP_BT_CFG: dict[str, Any] | None = None


def _backtest_pool_init(
    root_str: str,
    model_dir_rel: str,
    config_rel: str | None,
    config_overrides_json: str,
) -> None:
    global _MP_BT_PIPE, _MP_BT_CFG
    _finder_pool_init(root_str)
    root = Path(root_str)
    overrides = json.loads(config_overrides_json) if config_overrides_json else {}
    cfg = load_config(root, config_rel)
    if overrides:
        cfg = {**cfg, **overrides}
    _MP_BT_CFG = cfg
    _MP_BT_PIPE = load_model(root, model_dir_rel=model_dir_rel)


def _backtest_pair_task(task: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Process-pool worker: scan one pair for finder signals."""
    pair = task["pair"]
    assert _MP_BT_PIPE is not None and _MP_BT_CFG is not None
    try:
        ds = HistoricalDatastore(Path(task["datadir"]))
        tf = _MP_BT_CFG.get("timeframe", "5m")
        ohlcv = ds.load(pair, tf)
        ind = compute_indicator_frame(ohlcv, timeframe=tf)
        sigs, skipped = scan_pair(
            _MP_BT_PIPE,
            pair,
            ohlcv,
            ind,
            _MP_BT_CFG,
            start_ms=task.get("start_ms"),
            end_ms=task.get("end_ms"),
            classifier_gate=None,
            gate_scenario=None,
            gate_cfg=_MP_BT_CFG.get("classifier_gate") or {},
        )
        return pair, sigs, skipped, None
    except FileNotFoundError:
        return pair, [], [], "no OHLCV"
    except Exception as exc:
        return pair, [], [], str(exc)


def build_dataset(
    root: Path,
    pairs: list[str],
    cfg: dict[str, Any],
    *,
    timerange: str | None = None,
    workers: int = 1,
) -> pd.DataFrame:
    datadir = manifest_datadir(root)
    ds = HistoricalDatastore(datadir)
    tf = cfg.get("timeframe", "5m")
    start_ms, end_ms = parse_timerange_ms(timerange or cfg["train_timerange"])

    all_rows: list[dict[str, Any]] = []
    n_workers = max(1, int(workers or 1))

    if n_workers <= 1:
        for pair in pairs:
            try:
                ohlcv = ds.load(pair, tf)
            except FileNotFoundError:
                print(f"  skip {pair}: no OHLCV", flush=True)
                continue
            ind = compute_indicator_frame(ohlcv, timeframe=tf)
            samples = build_pair_samples(pair, ohlcv, ind, cfg, start_ms=start_ms, end_ms=end_ms)
            print(f"  {pair}: {len(samples)} samples", flush=True)
            all_rows.extend(samples)
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        tasks = [
            {
                "root": str(root),
                "datadir": str(datadir),
                "pair": pair,
                "cfg": cfg,
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
            for pair in pairs
        ]
        print(f"  building samples · {len(tasks)} pairs · {n_workers} workers", flush=True)
        done = 0
        with ProcessPoolExecutor(max_workers=n_workers, initializer=_finder_pool_init, initargs=(str(root),)) as pool:
            futs = [pool.submit(_collect_pair_samples_task, t) for t in tasks]
            for fut in as_completed(futs):
                pair, samples, err = fut.result()
                done += 1
                if err:
                    print(f"  skip {pair}: {err}", flush=True)
                else:
                    print(f"  {pair}: {len(samples)} samples", flush=True)
                    all_rows.extend(samples)
                if done % 20 == 0 or done == len(tasks):
                    print(f"    pairs {done}/{len(tasks)}", flush=True)

    if not all_rows:
        raise RuntimeError("no training samples built — check OHLCV data")
    return pd.DataFrame(all_rows)


def make_preprocessor() -> ColumnTransformer:
    return ColumnTransformer(
        transformers=[
            ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), CAT_FEATURES),
            ("num", SimpleImputer(strategy="median"), NUM_FEATURES),
        ]
    )


def make_pipeline(model_name: str = "xgboost", pos_rate: float = 0.4) -> Pipeline:
    if model_name != "xgboost":
        raise ValueError("trade_finder currently supports xgboost only")
    pre = make_preprocessor()
    spw = max(0.5, (1.0 - pos_rate) / max(pos_rate, 0.05))
    base = XGBClassifier(
        objective="binary:logistic",
        n_estimators=400,
        learning_rate=0.03,
        max_depth=6,
        min_child_weight=50,
        subsample=0.75,
        colsample_bytree=0.75,
        scale_pos_weight=spw,
        eval_metric="logloss",
        random_state=42,
        n_jobs=-1,
        verbosity=0,
    )
    clf = CalibratedClassifierCV(base, cv=3, method="isotonic")
    return Pipeline([("pre", _FramePreprocess(pre)), ("clf", clf)])


def time_split(df: pd.DataFrame, test_frac: float = 0.2) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.sort_values("open_ms")
    cut = int(len(df) * (1 - test_frac))
    return df.iloc[:cut], df.iloc[cut:]


def evaluate(pipe: Pipeline, train_df: pd.DataFrame, test_df: pd.DataFrame) -> dict[str, Any]:
    features = feature_columns()
    pipe.fit(train_df[features], train_df["label"])
    proba = pipe.predict_proba(test_df[features])
    pred = (proba[:, 1] >= 0.5).astype(int)
    y = test_df["label"].to_numpy()
    rep = classification_report(y, pred, target_names=["loss", "profit"], output_dict=True)
    pos_rate = float(y.mean()) if len(y) else 0.0
    return {
        "accuracy": round(float(accuracy_score(y, pred)), 4),
        "roc_auc": round(float(roc_auc_score(y, proba[:, 1])), 4) if len(set(y)) > 1 else None,
        "profit_recall": round(float(rep["profit"]["recall"]), 4),
        "profit_precision": round(float(rep["profit"]["precision"]), 4),
        "loss_recall": round(float(rep["loss"]["recall"]), 4),
        "loss_precision": round(float(rep["loss"]["precision"]), 4),
        "macro_f1": round(float(rep["macro avg"]["f1-score"]), 4),
        "positive_rate": round(pos_rate, 4),
        "confusion_matrix": confusion_matrix(y, pred).tolist(),
    }


def train_model(
    root: Path,
    *,
    config_rel: str | None = None,
    model_dir_rel: str | None = None,
    workers: int = 1,
) -> dict[str, Any]:
    cfg = load_config(root, config_rel)
    pairs = train_pairs(root, cfg)
    mode = cfg.get("train_mode", "triple_barrier")
    print(f"=== build dataset · mode={mode} · {len(pairs)} pairs ===", flush=True)
    if mode == "trade_db_mixed":
        df = build_dataset_from_trade_db(root, pairs, cfg)
    else:
        print(f"  stride {cfg['sample_stride']}", flush=True)
        df = build_dataset(root, pairs, cfg, workers=workers)
    train_df, test_df = time_split(df)
    print(f"=== train xgboost · {len(train_df)} train / {len(test_df)} test ===", flush=True)

    pipe = make_pipeline(cfg.get("model", "xgboost"), pos_rate=float(df["label"].mean()))
    metrics = evaluate(pipe, train_df, test_df)

    model_dir = root / (model_dir_rel or MODEL_DIR)
    model_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipe, model_dir / MODEL_FILE)

    out = {
        "model": "xgboost trade finder + isotonic calibration",
        "model_key": "xgboost",
        "task": mode,
        "n_samples": len(df),
        "n_train": len(train_df),
        "n_test": len(test_df),
        "pairs": pairs,
        "config": cfg,
        **metrics,
        "features": feature_columns(),
        "note": "Market-only features; ATR triple-barrier labels; time-based split",
        "trained_at": datetime.now(tz=UTC).isoformat(),
    }
    (model_dir / META_FILE).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def load_model(root: Path, *, model_dir_rel: str | None = None) -> Pipeline:
    model_dir = root / (model_dir_rel or MODEL_DIR)
    path = model_dir / MODEL_FILE
    if not path.is_file():
        raise FileNotFoundError(f"trade finder model not found: {path}")
    return joblib.load(path)


def predict_row(pipe: Pipeline, row: dict[str, Any]) -> dict[str, Any]:
    return predict_rows(pipe, [row])[0]


def predict_rows(pipe: Pipeline, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    features = feature_columns()
    x = pd.DataFrame([{k: row[k] for k in features} for row in rows])
    proba = pipe.predict_proba(x)
    out: list[dict[str, Any]] = []
    for j in range(len(rows)):
        p_loss, p_profit = float(proba[j, 0]), float(proba[j, 1])
        pred = "profit" if p_profit >= p_loss else "loss"
        out.append({
            "predicted": pred,
            "confidence_profit": round(p_profit, 4),
            "confidence_loss": round(p_loss, 4),
            "confidence": round(max(p_profit, p_loss), 4),
        })
    return out


def _scan_feature_rows(
    pair: str,
    merged: pd.DataFrame,
    indices: list[int],
) -> tuple[list[dict[str, Any]], list[tuple[int, bool]]]:
    """Build long+short feature rows for batch ML predict."""
    pb = pair_base(pair)
    feat_arrays = {col: merged[col].to_numpy(dtype=float) for col in MARKET_FEATURES if col in merged.columns}
    rows: list[dict[str, Any]] = []
    mapping: list[tuple[int, bool]] = []
    for i in indices:
        ts = merged.index[i]
        base: dict[str, Any] = {
            "pair_base": pb,
            "hour_utc": ts.hour,
            "dow_utc": ts.weekday(),
        }
        for col in MARKET_FEATURES:
            arr = feat_arrays.get(col)
            if arr is None:
                base[col] = float("nan")
            else:
                val = arr[i]
                base[col] = float(val) if not np.isnan(val) else float("nan")
        for is_short in (False, True):
            rows.append({**base, "is_short": "1" if is_short else "0"})
            mapping.append((i, is_short))
    return rows, mapping


def _batch_predictions(
    pipe: Pipeline,
    rows: list[dict[str, Any]],
    mapping: list[tuple[int, bool]],
    *,
    chunk_size: int = 4096,
) -> dict[int, list[tuple[bool, dict[str, Any]]]]:
    by_bar: dict[int, list[tuple[bool, dict[str, Any]]]] = {}
    for start in range(0, len(rows), chunk_size):
        chunk_rows = rows[start : start + chunk_size]
        chunk_map = mapping[start : start + chunk_size]
        for (bar_i, is_short), ml in zip(chunk_map, predict_rows(pipe, chunk_rows)):
            by_bar.setdefault(bar_i, []).append((is_short, ml))
    return by_bar


def scan_pair(
    pipe: Pipeline,
    pair: str,
    ohlcv: pd.DataFrame,
    ind: pd.DataFrame,
    cfg: dict[str, Any],
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    classifier_gate: Any | None = None,
    gate_scenario: dict[str, Any] | None = None,
    gate_cfg: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Walk bars and emit ML entry signals. Returns (kept, skipped_by_classifier)."""
    stride = int(cfg.get("scan_stride") or cfg.get("sample_stride") or 12)
    min_conf = float(cfg.get("min_confidence") or 0.55)
    cooldown = int(cfg.get("cooldown_bars") or 12)
    stake = float(cfg.get("stake_usdt") or 10)
    max_bars = int(cfg.get("max_bars") or 36)
    tp_mult = float(cfg.get("tp_atr_mult") or 1.5)
    sl_mult = float(cfg.get("sl_atr_mult") or 1.0)
    tf = cfg.get("timeframe", "5m")
    warmup = min_indicator_warmup(tf)

    merged = ohlcv.join(ind, how="inner")
    if start_ms is not None:
        merged = merged[merged.index >= pd.Timestamp(start_ms, unit="ms", tz="UTC")]
    if end_ms is not None:
        merged = merged[merged.index <= pd.Timestamp(end_ms, unit="ms", tz="UTC")]
    if len(merged) <= warmup + max_bars + 2:
        return [], []

    high = merged["high"].to_numpy(dtype=float)
    low = merged["low"].to_numpy(dtype=float)
    close = merged["close"].to_numpy(dtype=float)
    atr_pct = merged["atr_pct_14"].to_numpy(dtype=float)
    trend_arr = merged["trend_12h"].to_numpy(dtype=float) if "trend_12h" in merged.columns else None
    signals: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    next_allowed = warmup
    use_gate = classifier_gate is not None and gate_scenario is not None
    armed_offset = int((gate_cfg or {}).get("armed_offset_ms") or 3600000)

    end_i = len(merged) - max_bars - 1
    eligible: list[int] = []
    for i in range(warmup, end_i, stride):
        if np.isnan(atr_pct[i]) or atr_pct[i] <= 0:
            continue
        if trend_arr is not None and np.isnan(trend_arr[i]):
            continue
        eligible.append(i)

    feat_rows, feat_map = _scan_feature_rows(pair, merged, eligible)
    ml_by_bar = _batch_predictions(pipe, feat_rows, feat_map)

    for i in eligible:
        if i < next_allowed:
            continue
        ts = merged.index[i]
        open_ms = int(ts.timestamp() * 1000)

        best: dict[str, Any] | None = None
        for is_short, ml in ml_by_bar.get(i, ()):
            if ml["predicted"] != "profit" or ml["confidence_profit"] < min_conf:
                continue
            if best is None or ml["confidence_profit"] > best["ml"]["confidence_profit"]:
                best = {"ml": ml, "is_short": is_short}

        if best is None:
            continue

        if cfg.get("invert_signal"):
            best["is_short"] = not best["is_short"]

        pnl, hold_bars, reason = simulate_trade_pnl(
            high, low, close, atr_pct, i,
            is_short=best["is_short"], stake=stake,
            tp_mult=tp_mult, sl_mult=sl_mult, max_bars=max_bars,
        )
        candidate = {
            "pair": pair,
            "open_ms": open_ms,
            "open_rate": float(close[i]),
            "is_short": best["is_short"],
            "finder_ml": best["ml"],
            "ml": best["ml"],
            "profit_abs": pnl,
            "hold_bars": hold_bars,
            "exit_reason": reason,
        }
        if use_gate:
            from simulation.ml.finder_classifier import classify_signal

            keep, enriched = classify_signal(
                candidate,
                gate=classifier_gate,
                scenario=gate_scenario,
                finder_cfg=cfg,
                atr_pct=float(atr_pct[i]),
                armed_offset_ms=armed_offset,
            )
            if not keep:
                skipped.append(enriched)
                continue
            candidate = enriched
        signals.append(candidate)
        next_allowed = i + hold_bars + cooldown
    return signals, skipped


def backtest_pairs(
    root: Path,
    pairs: list[str],
    *,
    config_rel: str | None = None,
    model_dir_rel: str | None = None,
    config_overrides: dict[str, Any] | None = None,
    timerange: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    use_classifier_gate: bool | None = None,
    workers: int = 1,
) -> dict[str, Any]:
    cfg = load_config(root, config_rel)
    if config_overrides:
        cfg = {**cfg, **config_overrides}
    datadir = manifest_datadir(root)
    tf = cfg.get("timeframe", "5m")

    gate_cfg = cfg.get("classifier_gate") or {}
    if use_classifier_gate is None:
        use_classifier_gate = bool(gate_cfg.get("enabled"))
    classifier_gate = None
    gate_scenario = None
    if use_classifier_gate:
        from simulation.ml.finder_classifier import load_gate_scenario, make_gate

        sid = gate_cfg.get("scenario_id", "trend_ema")
        gate_scenario = load_gate_scenario(root, sid)
        classifier_gate = make_gate(root, gate_cfg)
        if not classifier_gate.status().get("ready"):
            raise RuntimeError("pnl_classifier not ready — run train_pnl_classifier.py first")

    if timerange:
        start_ms, end_ms = parse_timerange_ms(timerange)

    n_workers = max(1, int(workers or 1))
    parallel_ok = n_workers > 1 and not use_classifier_gate

    all_signals: list[dict[str, Any]] = []
    all_skipped: list[dict[str, Any]] = []
    by_pair: dict[str, list[dict[str, Any]]] = {}

    if parallel_ok:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        overrides_json = json.dumps(config_overrides or {})
        tasks = [
            {"pair": pair, "datadir": str(datadir), "start_ms": start_ms, "end_ms": end_ms}
            for pair in pairs
        ]
        print(f"  backtest · {len(tasks)} pairs · {n_workers} workers", flush=True)
        done = 0
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_backtest_pool_init,
            initargs=(str(root), model_dir_rel or MODEL_DIR, config_rel, overrides_json),
        ) as pool:
            futs = [pool.submit(_backtest_pair_task, t) for t in tasks]
            for fut in as_completed(futs):
                pair, sigs, skipped, err = fut.result()
                done += 1
                if err:
                    print(f"  skip {pair}: {err}", flush=True)
                else:
                    by_pair[pair] = sigs
                    all_signals.extend(sigs)
                    all_skipped.extend(skipped)
                    pnl = sum(s["profit_abs"] for s in sigs)
                    print(f"  {pair}: {len(sigs)} signals · {pnl:+.2f} USDT", flush=True)
                if done % 20 == 0 or done == len(tasks):
                    print(f"    pairs {done}/{len(tasks)}", flush=True)
    else:
        pipe = load_model(root, model_dir_rel=model_dir_rel)
        ds = HistoricalDatastore(datadir)
        for pair in pairs:
            try:
                print(f"  scanning {pair}...", flush=True)
                ohlcv = ds.load(pair, tf)
            except FileNotFoundError:
                print(f"  skip {pair}: no OHLCV", flush=True)
                continue
            ind = compute_indicator_frame(ohlcv, timeframe=tf)
            sigs, skipped = scan_pair(
                pipe, pair, ohlcv, ind, cfg,
                start_ms=start_ms, end_ms=end_ms,
                classifier_gate=classifier_gate,
                gate_scenario=gate_scenario,
                gate_cfg=gate_cfg,
            )
            by_pair[pair] = sigs
            all_signals.extend(sigs)
            all_skipped.extend(skipped)
            pnl = sum(s["profit_abs"] for s in sigs)
            skip_pnl = sum(s["profit_abs"] for s in skipped)
            extra = f" · gate skip {len(skipped)} ({skip_pnl:+.2f})" if use_classifier_gate else ""
            print(f"  {pair}: {len(sigs)} signals · {pnl:+.2f} USDT{extra}", flush=True)

    total_pnl = sum(s["profit_abs"] for s in all_signals)
    wins = sum(1 for s in all_signals if s["profit_abs"] >= 0)
    losses = len(all_signals) - wins
    skipped_pnl = sum(s["profit_abs"] for s in all_skipped)
    return {
        "pairs": pairs,
        "timerange": timerange,
        "config": cfg,
        "classifier_gate": use_classifier_gate,
        "gate_scenario": gate_scenario.get("id") if gate_scenario else None,
        "total_signals": len(all_signals),
        "gate_skipped": len(all_skipped),
        "skipped_pnl_usdt": round(skipped_pnl, 4),
        "total_pnl_usdt": round(total_pnl, 4),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(all_signals), 4) if all_signals else 0,
        "by_pair": {
            p: {
                "signals": len(sigs),
                "pnl_usdt": round(sum(x["profit_abs"] for x in sigs), 4),
                "wins": sum(1 for x in sigs if x["profit_abs"] >= 0),
            }
            for p, sigs in by_pair.items()
        },
        "signals": all_signals,
    }

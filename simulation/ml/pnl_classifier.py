"""Profit vs loss classifier for trade_db entries (entry-time features only)."""
from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sklearn.base import BaseEstimator, TransformerMixin

from simulation.ml.market_features import (
    MARKET_FEATURES,
    MarketFeatureStore,
    manifest_datadir,
)

DB_DIR = "simulation/results/trade_db"
MODEL_DIR = "simulation/results/trade_db/models"
MODEL_FILE = "pnl_classifier.joblib"
META_FILE = "pnl_classifier_meta.json"

CAT_FEATURES = [
    "scenario_id",
    "pair_base",
    "scan_type",
    "group",
    "strategy",
    "is_short",
]
NUM_FEATURES = [
    "stake_usdt",
    "stoploss",
    "roi_at_entry",
    "hour_utc",
    "dow_utc",
    "mins_since_armed",
    "log_open_rate",
] + MARKET_FEATURES


def feature_columns() -> list[str]:
    return CAT_FEATURES + NUM_FEATURES


def pair_base(pair: str) -> str:
    return (pair or "").split("/")[0]


def load_trades(db_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sub in ("profit", "loss"):
        d = db_root / sub
        if not d.is_dir():
            continue
        for fp in d.glob("*.json"):
            try:
                rec = json.loads(fp.read_text(encoding="utf-8"))
                rec["_folder"] = sub
                rows.append(rec)
            except (json.JSONDecodeError, OSError):
                continue
    return rows


def trade_dedup_key(rec: dict[str, Any]) -> tuple[Any, ...]:
    tr = rec.get("trade") or {}
    basis = rec.get("basis") or {}
    sid = rec.get("scenario_id") or basis.get("scenario_id") or ""
    pair = rec.get("pair") or basis.get("pair") or ""
    return (sid, pair, tr.get("open_ms"), bool(tr.get("is_short")))


def export_record_to_train_rec(rec: dict[str, Any]) -> dict[str, Any]:
    """Convert full_ml_study export JSONL row to trade_db training shape."""
    ic = rec.get("inst_config") or {}
    tr = rec.get("trade") or {}
    return {
        "scenario_id": rec.get("scenario_id"),
        "pair": rec.get("pair"),
        "profit_abs": rec.get("profit_abs"),
        "trade": tr,
        "basis": {
            "scenario_id": rec.get("scenario_id"),
            "pair": rec.get("pair"),
            "scan_type": rec.get("scan_type") or "strategy",
            "group": rec.get("group") or "unknown",
            "strategy": rec.get("strategy") or "unknown",
            "stake_usdt": ic.get("stake"),
            "stoploss": ic.get("stoploss"),
            "minimal_roi": ic.get("minimal_roi"),
            "timeframe": ic.get("timeframe", "5m"),
            "armed_at_ms": rec.get("armed_at_ms"),
        },
    }


def load_trades_from_export_dir(export_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in ("all_wins.jsonl", "all_losses.jsonl"):
        path = export_dir / name
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(export_record_to_train_rec(json.loads(line)))
    return rows


def load_all_training_trades(root: Path, export_dir: Path | None = None) -> list[dict[str, Any]]:
    """Merge trade_db + full_ml_study export (dedup by scenario/pair/open_ms/side)."""
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for t in load_trades(root / DB_DIR):
        merged[trade_dedup_key(t)] = t
    if export_dir and export_dir.is_dir():
        for t in load_trades_from_export_dir(export_dir):
            merged[trade_dedup_key(t)] = t
    return list(merged.values())


def trade_to_features(rec: dict[str, Any]) -> dict[str, Any]:
    """Features known at trade entry (no close price / PnL / exit reason)."""
    basis = rec.get("basis") or {}
    tr = rec.get("trade") or {}
    open_ms = int(tr.get("open_ms") or 0)
    armed = int(basis.get("armed_at_ms") or basis.get("scan_armed_at_ms") or open_ms)
    dt = datetime.fromtimestamp(open_ms / 1000, tz=UTC) if open_ms else None
    roi = basis.get("minimal_roi") or {}
    roi0 = float(roi.get("0") or roi.get(0) or 0)
    open_rate = float(tr.get("open_rate") or 0)
    label = 1 if float(rec.get("profit_abs") or 0) >= 0 else 0
    pair = rec.get("pair") or basis.get("pair") or ""
    return {
        "id": rec.get("id"),
        "label": label,
        "pair": pair,
        "timeframe": basis.get("timeframe") or "5m",
        "scenario_id": rec.get("scenario_id") or basis.get("scenario_id"),
        "pair_base": pair_base(pair),
        "scan_type": basis.get("scan_type") or "unknown",
        "group": basis.get("group") or "unknown",
        "strategy": basis.get("strategy") or "unknown",
        "is_short": "1" if tr.get("is_short") else "0",
        "stake_usdt": float(basis.get("stake_usdt") or 0),
        "stoploss": float(basis.get("stoploss") or 0),
        "roi_at_entry": roi0,
        "hour_utc": dt.hour if dt else 0,
        "dow_utc": dt.weekday() if dt else 0,
        "mins_since_armed": max(0.0, (open_ms - armed) / 60000.0) if open_ms else 0.0,
        "log_open_rate": float(np.log1p(open_rate)) if open_rate > 0 else 0.0,
        "open_ms": open_ms,
    }


def build_dataframe(trades: list[dict[str, Any]], market_store: MarketFeatureStore | None = None) -> pd.DataFrame:
    df = pd.DataFrame([trade_to_features(t) for t in trades])
    if market_store is not None:
        df = market_store.enrich_dataframe(df)
    else:
        for col in MARKET_FEATURES:
            df[col] = np.nan
    return df


def make_market_store(root: Path) -> MarketFeatureStore:
    return MarketFeatureStore(manifest_datadir(root))


class _FramePreprocess(BaseEstimator, TransformerMixin):
    """ColumnTransformer -> pandas DataFrame with stable feature names."""

    def __init__(self, preprocessor: ColumnTransformer):
        self.preprocessor = preprocessor

    def fit(self, X, y=None):
        self.preprocessor.fit(X, y)
        self.columns_ = list(self.preprocessor.get_feature_names_out())
        return self

    def transform(self, X):
        arr = self.preprocessor.transform(X)
        idx = X.index if isinstance(X, pd.DataFrame) else None
        return pd.DataFrame(arr, columns=self.columns_, index=idx)


def make_preprocessor() -> ColumnTransformer:
    return ColumnTransformer(
        transformers=[
            (
                "cat",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                CAT_FEATURES,
            ),
            ("num", SimpleImputer(strategy="median"), NUM_FEATURES),
        ]
    )


def gpu_available() -> bool:
    """True when NVIDIA GPU should be used for tree boosters (auto or ML_USE_GPU=1)."""
    flag = os.environ.get("ML_USE_GPU", "").strip().lower()
    if flag in ("0", "false", "no"):
        return False
    if flag in ("1", "true", "yes"):
        return True
    return shutil.which("nvidia-smi") is not None


def model_registry() -> dict[str, Any]:
    """Named base estimators for profit/loss classification."""
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from xgboost import XGBClassifier

    use_gpu = gpu_available()
    lgbm_kw: dict[str, Any] = dict(
        objective="binary",
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
    if use_gpu:
        lgbm_kw["device"] = "gpu"

    xgb_kw: dict[str, Any] = dict(
        objective="binary:logistic",
        n_estimators=400,
        learning_rate=0.05,
        max_depth=8,
        min_child_weight=25,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=1.0,
        eval_metric="logloss",
        random_state=42,
        n_jobs=-1,
        verbosity=0,
    )
    if use_gpu:
        try:
            probe = XGBClassifier(tree_method="gpu_hist", device="cuda", n_estimators=1, verbosity=0)
            _x = np.random.rand(32, 4)
            _y = (np.random.rand(32) > 0.5).astype(int)
            probe.fit(_x, _y)
            xgb_kw["tree_method"] = "gpu_hist"
            xgb_kw["device"] = "cuda"
        except Exception:
            pass

    return {
        "lightgbm": LGBMClassifier(**lgbm_kw),
        "xgboost": XGBClassifier(**xgb_kw),
        "hist_gbm": HistGradientBoostingClassifier(
            max_iter=400,
            learning_rate=0.05,
            max_depth=8,
            min_samples_leaf=25,
            class_weight="balanced",
            random_state=42,
        ),
        "random_forest": RandomForestClassifier(
            n_estimators=300,
            max_depth=12,
            min_samples_leaf=25,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
        ),
        "logistic": LogisticRegression(
            max_iter=2000,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
        ),
    }


def make_pipeline(model_name: str = "lightgbm", *, calibrate: bool = True) -> Pipeline:
    registry = model_registry()
    if model_name not in registry:
        raise ValueError(f"unknown model: {model_name}; choose from {sorted(registry)}")
    pre = make_preprocessor()
    base = registry[model_name]
    clf: Any = base
    if calibrate:
        clf = CalibratedClassifierCV(base, cv=3, method="isotonic")
    return Pipeline([("pre", _FramePreprocess(pre)), ("clf", clf)])


def time_split(df: pd.DataFrame, test_frac: float = 0.2) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.sort_values("open_ms")
    cut = int(len(df) * (1 - test_frac))
    return df.iloc[:cut], df.iloc[cut:]


def evaluate_pipeline(pipe: Pipeline, train_df: pd.DataFrame, test_df: pd.DataFrame) -> dict[str, Any]:
    features = feature_columns()
    pipe.fit(train_df[features], train_df["label"])
    proba = pipe.predict_proba(test_df[features])
    classes = list(getattr(pipe.named_steps["clf"], "classes_", [0, 1]))
    profit_idx = classes.index(1) if 1 in classes else len(classes) - 1
    pred = (proba[:, profit_idx] >= 0.5).astype(int)
    y = test_df["label"].to_numpy()
    rep = classification_report(
        y,
        pred,
        labels=[0, 1],
        target_names=["loss", "profit"],
        output_dict=True,
        zero_division=0,
    )
    return {
        "accuracy": round(float(accuracy_score(y, pred)), 4),
        "roc_auc": round(float(roc_auc_score(y, proba[:, profit_idx])), 4) if len(set(y)) > 1 else None,
        "loss_recall": round(float(rep["loss"]["recall"]), 4),
        "loss_precision": round(float(rep["loss"]["precision"]), 4),
        "profit_recall": round(float(rep["profit"]["recall"]), 4),
        "profit_precision": round(float(rep["profit"]["precision"]), 4),
        "macro_f1": round(float(rep["macro avg"]["f1-score"]), 4),
        "confusion_matrix": confusion_matrix(y, pred, labels=[0, 1]).tolist(),
        "classification_report": rep,
    }


def prepare_dataset(root: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    db_root = root / DB_DIR
    trades = load_trades(db_root)
    if len(trades) < 50:
        raise RuntimeError(f"not enough trades to train: {len(trades)}")
    return prepare_dataset_from_trades(root, trades)


def prepare_dataset_from_trades(
    root: Path, trades: list[dict[str, Any]]
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    if len(trades) < 50:
        raise RuntimeError(f"not enough trades to train: {len(trades)}")
    market_store = make_market_store(root)
    df = build_dataframe(trades, market_store)
    train_df, test_df = time_split(df)
    meta = {
        "n_trades": len(df),
        "n_train": len(train_df),
        "n_test": len(test_df),
        "market_coverage": market_store.coverage(df),
    }
    return train_df, test_df, meta


def compare_models(root: Path, models: list[str] | None = None) -> dict[str, Any]:
    models = models or list(model_registry().keys())
    train_df, test_df, meta = prepare_dataset(root)
    rows: list[dict[str, Any]] = []
    for name in models:
        label = f"{name} + isotonic"
        print(f"--- train {label} ---")
        try:
            pipe = make_pipeline(name, calibrate=True)
            metrics = evaluate_pipeline(pipe, train_df, test_df)
            rows.append({"model": name, "label": label, **metrics, "error": None})
        except Exception as exc:
            rows.append({"model": name, "label": label, "error": str(exc)})
    ok = [r for r in rows if not r.get("error")]
    ok.sort(key=lambda r: (r.get("roc_auc") or 0, r.get("loss_recall") or 0), reverse=True)
    best = ok[0] if ok else None
    out = {
        "compared_at": datetime.now(tz=UTC).isoformat(),
        **meta,
        "ranking": ok,
        "all_results": rows,
        "best_model": best["model"] if best else None,
        "note": "Time-based split; isotonic calibration; ranked by ROC-AUC then loss recall",
    }
    out_path = root / MODEL_DIR / "model_comparison.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def train_model(root: Path, model_name: str = "lightgbm") -> dict[str, Any]:
    db_root = root / DB_DIR
    trades = load_trades(db_root)
    return train_model_on_trades(
        root,
        trades,
        model_name,
        note="trade_db only; time-based split (older=train, newer=test)",
        train_source="trade_db",
    )


def train_model_on_trades(
    root: Path,
    trades: list[dict[str, Any]],
    model_name: str = "lightgbm",
    *,
    note: str = "",
    train_source: str = "custom",
) -> dict[str, Any]:
    model_dir = root / MODEL_DIR
    model_dir.mkdir(parents=True, exist_ok=True)

    train_df, test_df, meta = prepare_dataset_from_trades(root, trades)
    label = f"{model_name} + isotonic calibration"

    pipe = make_pipeline(model_name, calibrate=True)
    metrics = evaluate_pipeline(pipe, train_df, test_df)
    features = feature_columns()

    out = {
        "model": label,
        "model_key": model_name,
        "train_source": train_source,
        "n_trades": meta["n_trades"],
        "n_train": meta["n_train"],
        "n_test": meta["n_test"],
        "market_coverage": meta["market_coverage"],
        **metrics,
        "features": features,
        "market_features": MARKET_FEATURES,
        "note": note or "Entry-time + market indicators (5m); time-based split (older=train, newer=test)",
        "trained_at": datetime.now(tz=UTC).isoformat(),
    }

    joblib.dump(pipe, model_dir / MODEL_FILE)
    meta_path = model_dir / META_FILE
    meta_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def load_model(root: Path) -> Pipeline:
    path = root / MODEL_DIR / MODEL_FILE
    if not path.is_file():
        raise FileNotFoundError(f"model not found: {path}")
    return joblib.load(path)


def _proba_to_result(
    rec: dict[str, Any],
    p_loss: float,
    p_profit: float,
    *,
    include_actual: bool = True,
) -> dict[str, Any]:
    pred = "profit" if p_profit >= p_loss else "loss"
    out: dict[str, Any] = {
        "id": rec.get("id"),
        "scenario_id": rec.get("scenario_id"),
        "pair": rec.get("pair"),
        "label": rec.get("label"),
        "predicted": pred,
        "confidence_profit": round(p_profit, 4),
        "confidence_loss": round(p_loss, 4),
        "confidence": round(max(p_profit, p_loss), 4),
    }
    if include_actual and rec.get("profit_abs") is not None:
        actual = "profit" if float(rec.get("profit_abs") or 0) >= 0 else "loss"
        out["actual"] = actual
        out["correct"] = pred == actual
    return out


def _features_row(rec: dict[str, Any], market_store: MarketFeatureStore | None = None) -> dict[str, Any]:
    row = trade_to_features(rec)
    if market_store is not None:
        mkt = market_store.features_at(row["pair"], int(row["open_ms"]), row.get("timeframe") or "5m")
        row.update(mkt)
    else:
        for col in MARKET_FEATURES:
            row[col] = float("nan")
    return row


def predict_trade(
    pipe: Pipeline,
    rec: dict[str, Any],
    market_store: MarketFeatureStore | None = None,
) -> dict[str, Any]:
    row = _features_row(rec, market_store)
    features = feature_columns()
    x = pd.DataFrame([{k: row[k] for k in features}])
    proba = pipe.predict_proba(x)[0]
    return _proba_to_result(rec, float(proba[0]), float(proba[1]))


def predict_entry(
    pipe: Pipeline,
    body: dict[str, Any],
    market_store: MarketFeatureStore | None = None,
) -> dict[str, Any]:
    """Score a trade at entry from scenario + basis + open fields."""
    rec = {
        "id": body.get("id"),
        "scenario_id": body.get("scenario_id"),
        "pair": body.get("pair"),
        "label": body.get("label"),
        "profit_abs": body.get("profit_abs"),
        "basis": body.get("basis") or {},
        "trade": body.get("trade") or {},
    }
    basis = rec["basis"]
    tr = rec["trade"]
    if not tr.get("open_ms") and body.get("open_ms"):
        tr["open_ms"] = body["open_ms"]
    if not tr.get("open_rate") and body.get("open_rate"):
        tr["open_rate"] = body["open_rate"]
    if "is_short" not in tr and "is_short" in body:
        tr["is_short"] = body["is_short"]
    if not basis.get("scenario_id"):
        basis["scenario_id"] = rec.get("scenario_id")
    if not basis.get("pair"):
        basis["pair"] = rec.get("pair")
    if not basis.get("timeframe") and body.get("timeframe"):
        basis["timeframe"] = body["timeframe"]
    return predict_trade(pipe, rec, market_store)


def score_all(root: Path) -> dict[str, Any]:
    db_root = root / DB_DIR
    pipe = load_model(root)
    market_store = make_market_store(root)
    trades = load_trades(db_root)
    preds = [predict_trade(pipe, t, market_store) for t in trades]
    correct = sum(1 for p in preds if p["correct"])
    out = {
        "scored_at": datetime.now(tz=UTC).isoformat(),
        "total": len(preds),
        "accuracy_on_all": round(correct / len(preds), 4) if preds else 0,
        "predictions": preds,
    }
    out_path = root / MODEL_DIR / "predictions.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out

#!/usr/bin/env python3
"""15 ML hyperparameter experiments on all TA scenarios × all pairs.

Train: all history before April cut. Test: from April onwards.
Caches collected trades so backtests are not re-run on resume.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SIM_SKIP_PERSIST", "1")

import numpy as np  # noqa: E402
from lightgbm import LGBMClassifier  # noqa: E402
from sklearn.base import BaseEstimator, TransformerMixin, clone  # noqa: E402
from sklearn.calibration import CalibratedClassifierCV  # noqa: E402
from sklearn.compose import ColumnTransformer  # noqa: E402
from sklearn.decomposition import PCA  # noqa: E402
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis  # noqa: E402
from sklearn.ensemble import (  # noqa: E402
    AdaBoostClassifier,
    BaggingClassifier,
    ExtraTreesClassifier,
    GradientBoostingClassifier,
    HistGradientBoostingClassifier,
    RandomForestClassifier,
    VotingClassifier,
)
from sklearn.feature_selection import SelectKBest, f_classif  # noqa: E402
from sklearn.impute import SimpleImputer  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.naive_bayes import GaussianNB  # noqa: E402
from sklearn.neural_network import MLPClassifier  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402
from sklearn.preprocessing import (  # noqa: E402
    OneHotEncoder,
    QuantileTransformer,
    RobustScaler,
    StandardScaler,
)
from xgboost import XGBClassifier  # noqa: E402

from simulation.exchange_sim.bot_session import BotSessionManager  # noqa: E402
from simulation.ml.market_features import MARKET_FEATURES  # noqa: E402
from simulation.ml.pnl_classifier import (  # noqa: E402
    CAT_FEATURES,
    NUM_FEATURES,
    _FramePreprocess,
    build_dataframe,
    evaluate_pipeline,
    feature_columns,
    make_market_store,
    make_preprocessor,
)
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402
from simulation.scripts.run_scalp_strategies_compare import (  # noqa: E402
    CHART2_SCENARIOS,
    CHART3_SCENARIOS,
    CHART_SCENARIOS,
    COMBO_SCENARIOS,
    NEWSET_SCENARIOS,
    SCALP_SCENARIOS,
    apply_ml_gate,
    collect_train_test_trades,
    summarize,
    timerange_to_ms,
)

ALL_SCENARIOS = (
    SCALP_SCENARIOS
    + NEWSET_SCENARIOS
    + CHART_SCENARIOS
    + CHART2_SCENARIOS
    + CHART3_SCENARIOS
    + COMBO_SCENARIOS
)

DEFAULT_TRAIN = "20250101-20260331"
DEFAULT_TEST = "20260401-20260625"
DEFAULT_OUT = ROOT / "simulation/results/ml_param_experiments"


def build_clf(spec: dict[str, Any]) -> Any:
    kind = spec["model"]
    params = {k: v for k, v in dict(spec.get("params") or {}).items() if v is not None}
    if kind == "lightgbm":
        kw = dict(
            objective="binary",
            random_state=42,
            verbose=-1,
            n_jobs=-1,
            **params,
        )
        return LGBMClassifier(**kw)
    if kind == "xgboost":
        kw = dict(
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=42,
            n_jobs=-1,
            verbosity=0,
            **params,
        )
        return XGBClassifier(**kw)
    if kind == "hist_gbm":
        return HistGradientBoostingClassifier(random_state=42, **params)
    if kind == "random_forest":
        return RandomForestClassifier(random_state=42, n_jobs=-1, **params)
    if kind == "extra_trees":
        return ExtraTreesClassifier(random_state=42, n_jobs=-1, **params)
    if kind == "logistic":
        return LogisticRegression(max_iter=2000, random_state=42, n_jobs=-1, **params)
    if kind == "gradient_boosting":
        return GradientBoostingClassifier(random_state=42, **params)
    if kind == "adaboost":
        return AdaBoostClassifier(random_state=42, **params)
    if kind == "mlp":
        return MLPClassifier(random_state=42, max_iter=400, **params)
    if kind == "gaussian_nb":
        return GaussianNB(**params)
    if kind == "lda":
        return LinearDiscriminantAnalysis(**params)
    if kind == "bagging_lgbm":
        base = LGBMClassifier(
            objective="binary",
            random_state=42,
            verbose=-1,
            n_jobs=1,
            n_estimators=int(params.pop("base_n_estimators", 200)),
            learning_rate=float(params.pop("base_learning_rate", 0.05)),
            max_depth=int(params.pop("base_max_depth", 6)),
            num_leaves=int(params.pop("base_num_leaves", 31)),
            class_weight="balanced",
        )
        return BaggingClassifier(
            estimator=base,
            n_estimators=int(params.pop("n_estimators", 8)),
            max_samples=float(params.pop("max_samples", 0.8)),
            max_features=float(params.pop("max_features", 0.8)),
            random_state=42,
            n_jobs=-1,
            **params,
        )
    if kind == "voting_soft":
        # Soft vote of three diverse trees; params unused (fixed ensemble).
        lgbm = LGBMClassifier(
            objective="binary",
            n_estimators=300,
            learning_rate=0.05,
            max_depth=6,
            num_leaves=31,
            class_weight="balanced",
            random_state=42,
            verbose=-1,
            n_jobs=-1,
        )
        xgb = XGBClassifier(
            objective="binary:logistic",
            eval_metric="logloss",
            n_estimators=300,
            learning_rate=0.05,
            max_depth=6,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            n_jobs=-1,
            verbosity=0,
        )
        hgb = HistGradientBoostingClassifier(
            max_iter=300,
            learning_rate=0.05,
            max_depth=6,
            class_weight="balanced",
            random_state=42,
        )
        return VotingClassifier(
            estimators=[("lgbm", lgbm), ("xgb", xgb), ("hgb", hgb)],
            voting="soft",
            n_jobs=-1,
        )
    raise ValueError(f"unknown model kind: {kind}")


class _WinsorizeNumeric(BaseEstimator, TransformerMixin):
    """Clip numeric columns to train-set quantiles (default 1–99%)."""

    def __init__(self, lower: float = 0.01, upper: float = 0.99, columns: list[str] | None = None):
        self.lower = lower
        self.upper = upper
        self.columns = columns

    def fit(self, X, y=None):
        import pandas as pd

        df = X if isinstance(X, pd.DataFrame) else pd.DataFrame(X)
        cols = self.columns or list(df.columns)
        self.bounds_: dict[str, tuple[float, float]] = {}
        for c in cols:
            if c not in df.columns:
                continue
            s = pd.to_numeric(df[c], errors="coerce")
            lo = float(s.quantile(self.lower))
            hi = float(s.quantile(self.upper))
            if np.isfinite(lo) and np.isfinite(hi) and lo < hi:
                self.bounds_[c] = (lo, hi)
        return self

    def transform(self, X):
        import pandas as pd

        df = X.copy() if isinstance(X, pd.DataFrame) else pd.DataFrame(X)
        for c, (lo, hi) in self.bounds_.items():
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").clip(lo, hi)
        return df


def _ohe() -> OneHotEncoder:
    return OneHotEncoder(handle_unknown="ignore", sparse_output=False)


def build_preprocessor(spec: dict[str, Any]) -> Any:
    """Build ColumnTransformer (+ optional PCA/SelectKBest) from preprocess key."""
    mode = str(spec.get("preprocess") or "default")
    cat = list(CAT_FEATURES)
    num = list(NUM_FEATURES)
    if mode == "market_only":
        cat = ["is_short"]
        num = list(MARKET_FEATURES)
    elif mode == "num_only":
        cat = ["is_short"]
        num = list(NUM_FEATURES)
    elif mode == "no_time":
        cat = [c for c in CAT_FEATURES if c not in ("hour_utc", "dow_utc")]
        # hour/dow are numeric in NUM_FEATURES
        num = [c for c in NUM_FEATURES if c not in ("hour_utc", "dow_utc", "mins_since_armed")]

    if mode in ("default", "market_only", "num_only", "no_time"):
        ct = ColumnTransformer(
            transformers=[
                ("cat", _ohe(), cat),
                ("num", SimpleImputer(strategy="median"), num),
            ]
        )
        return ct

    if mode == "standard":
        return ColumnTransformer(
            transformers=[
                ("cat", _ohe(), cat),
                (
                    "num",
                    Pipeline(
                        [
                            ("impute", SimpleImputer(strategy="median")),
                            ("scale", StandardScaler()),
                        ]
                    ),
                    num,
                ),
            ]
        )
    if mode == "robust":
        return ColumnTransformer(
            transformers=[
                ("cat", _ohe(), cat),
                (
                    "num",
                    Pipeline(
                        [
                            ("impute", SimpleImputer(strategy="median")),
                            ("scale", RobustScaler()),
                        ]
                    ),
                    num,
                ),
            ]
        )
    if mode == "quantile":
        return ColumnTransformer(
            transformers=[
                ("cat", _ohe(), cat),
                (
                    "num",
                    Pipeline(
                        [
                            ("impute", SimpleImputer(strategy="median")),
                            (
                                "qt",
                                QuantileTransformer(
                                    output_distribution="normal",
                                    n_quantiles=200,
                                    subsample=100_000,
                                    random_state=42,
                                ),
                            ),
                        ]
                    ),
                    num,
                ),
            ]
        )
    if mode == "winsor":
        # Winsorize applied inside a custom first step of the outer Pipeline.
        return ColumnTransformer(
            transformers=[
                ("cat", _ohe(), cat),
                ("num", SimpleImputer(strategy="median"), num),
            ]
        )
    if mode == "pca24":
        return Pipeline(
            [
                (
                    "ct",
                    ColumnTransformer(
                        transformers=[
                            ("cat", _ohe(), cat),
                            ("num", SimpleImputer(strategy="median"), num),
                        ]
                    ),
                ),
                ("pca", PCA(n_components=24, random_state=42)),
            ]
        )
    if mode == "kbest40":
        return Pipeline(
            [
                (
                    "ct",
                    ColumnTransformer(
                        transformers=[
                            ("cat", _ohe(), cat),
                            ("num", SimpleImputer(strategy="median"), num),
                        ]
                    ),
                ),
                ("sel", SelectKBest(score_func=f_classif, k=40)),
            ]
        )
    # Fallback
    return make_preprocessor()


def make_exp_pipeline(spec: dict[str, Any]) -> Pipeline:
    base = build_clf(spec)
    clf: Any = base
    if spec.get("calibrate", True):
        # Default sigmoid: smooth confidence % for live gate UI (isotonic often → 0/1).
        method = str(spec.get("calibrate_method") or "sigmoid")
        clf = CalibratedClassifierCV(base, cv=3, method=method)

    mode = str(spec.get("preprocess") or "default")
    steps: list[tuple[str, Any]] = []
    if mode == "winsor":
        steps.append(("winsor", _WinsorizeNumeric(columns=list(NUM_FEATURES))))
    pre = build_preprocessor(spec)
    # PCA/SelectKBest return ndarray — skip _FramePreprocess name wrapping.
    if mode in ("pca24", "kbest40"):
        steps.append(("pre", pre))
    else:
        steps.append(("pre", _FramePreprocess(pre)))
    steps.append(("clf", clf))
    return Pipeline(steps)


EXPERIMENTS_BATCH1: list[dict[str, Any]] = [
    {
        "id": "exp01_baseline_lgbm",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp02_lgbm_no_balance",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "class_weight": None,
        },
    },
    {
        "id": "exp03_lgbm_shallow",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 4,
            "num_leaves": 15,
            "min_child_samples": 40,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp04_lgbm_deep",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 500,
            "learning_rate": 0.04,
            "max_depth": 12,
            "num_leaves": 127,
            "min_child_samples": 15,
            "subsample": 0.85,
            "colsample_bytree": 0.7,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp05_lgbm_slow_long",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 800,
            "learning_rate": 0.025,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp06_lgbm_fast_short",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 150,
            "learning_rate": 0.12,
            "max_depth": 6,
            "num_leaves": 31,
            "min_child_samples": 25,
            "subsample": 0.9,
            "colsample_bytree": 0.9,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp07_lgbm_leaf_heavy",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": -1,
            "num_leaves": 95,
            "min_child_samples": 10,
            "subsample": 0.75,
            "colsample_bytree": 0.75,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp08_lgbm_regularized",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 6,
            "num_leaves": 31,
            "min_child_samples": 60,
            "reg_alpha": 0.5,
            "reg_lambda": 2.0,
            "subsample": 0.7,
            "colsample_bytree": 0.7,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp09_xgb_baseline",
        "model": "xgboost",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "min_child_weight": 25,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "scale_pos_weight": 1.0,
        },
    },
    {
        "id": "exp10_xgb_pos_weight",
        "model": "xgboost",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 6,
            "min_child_weight": 20,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "scale_pos_weight": 1.5,
        },
    },
    {
        "id": "exp11_hist_gbm",
        "model": "hist_gbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "max_iter": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "min_samples_leaf": 25,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp12_random_forest",
        "model": "random_forest",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 400,
            "max_depth": 14,
            "min_samples_leaf": 20,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp13_logistic",
        "model": "logistic",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {"class_weight": "balanced", "C": 0.5},
    },
    {
        "id": "exp14_lgbm_gate045",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.45,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp15_lgbm_gate065_nocal",
        "model": "lightgbm",
        "calibrate": False,
        "min_profit_proba": 0.65,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "class_weight": "balanced",
        },
    },
]

# Second wave — different hyperparams / gates / model families. Reuses trade_cache.
EXPERIMENTS_BATCH2: list[dict[str, Any]] = [
    {
        "id": "exp16_lgbm_bagging5",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 450,
            "learning_rate": 0.05,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "subsample": 0.75,
            "subsample_freq": 5,
            "colsample_bytree": 0.8,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp17_lgbm_maxbin127",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "max_bin": 127,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp18_lgbm_maxbin63",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "max_bin": 63,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp19_lgbm_subsample05",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 500,
            "learning_rate": 0.05,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "subsample": 0.5,
            "subsample_freq": 1,
            "colsample_bytree": 0.8,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp20_lgbm_colsample05",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 500,
            "learning_rate": 0.05,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "subsample": 0.8,
            "colsample_bytree": 0.5,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp21_lgbm_is_unbalance",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "is_unbalance": True,
        },
    },
    {
        "id": "exp22_xgb_shallow",
        "model": "xgboost",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.06,
            "max_depth": 4,
            "min_child_weight": 40,
            "subsample": 0.85,
            "colsample_bytree": 0.85,
            "scale_pos_weight": 1.0,
        },
    },
    {
        "id": "exp23_xgb_deep",
        "model": "xgboost",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 500,
            "learning_rate": 0.04,
            "max_depth": 10,
            "min_child_weight": 10,
            "subsample": 0.8,
            "colsample_bytree": 0.7,
            "reg_lambda": 1.5,
            "scale_pos_weight": 1.2,
        },
    },
    {
        "id": "exp24_xgb_slow",
        "model": "xgboost",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 900,
            "learning_rate": 0.02,
            "max_depth": 7,
            "min_child_weight": 25,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "scale_pos_weight": 1.0,
        },
    },
    {
        "id": "exp25_hist_gbm_shallow",
        "model": "hist_gbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "max_iter": 350,
            "learning_rate": 0.08,
            "max_depth": 4,
            "min_samples_leaf": 40,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp26_hist_gbm_deep",
        "model": "hist_gbm",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "max_iter": 500,
            "learning_rate": 0.04,
            "max_depth": 12,
            "min_samples_leaf": 15,
            "l2_regularization": 0.5,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp27_extra_trees",
        "model": "extra_trees",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 450,
            "max_depth": 16,
            "min_samples_leaf": 15,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp28_rf_deep500",
        "model": "random_forest",
        "calibrate": True,
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 500,
            "max_depth": 18,
            "min_samples_leaf": 12,
            "max_features": "sqrt",
            "class_weight": "balanced_subsample",
        },
    },
    {
        "id": "exp29_lgbm_gate050",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.50,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp30_lgbm_gate070",
        "model": "lightgbm",
        "calibrate": True,
        "min_profit_proba": 0.70,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "class_weight": "balanced",
        },
    },
]

# Third wave — new model families + feature preprocessing (top-30 focus).
_LGBM_BASE = {
    "n_estimators": 400,
    "learning_rate": 0.05,
    "max_depth": 8,
    "num_leaves": 63,
    "min_child_samples": 25,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "class_weight": "balanced",
}

EXPERIMENTS_BATCH3: list[dict[str, Any]] = [
    {
        "id": "exp31_mlp",
        "model": "mlp",
        "calibrate": True,
        "preprocess": "standard",
        "min_profit_proba": 0.55,
        "params": {
            "hidden_layer_sizes": (64, 32),
            "alpha": 1e-3,
            "learning_rate_init": 0.001,
            "early_stopping": True,
            "validation_fraction": 0.1,
        },
    },
    {
        "id": "exp32_sklearn_gbdt",
        "model": "gradient_boosting",
        "calibrate": True,
        "preprocess": "default",
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 300,
            "learning_rate": 0.05,
            "max_depth": 4,
            "min_samples_leaf": 30,
            "subsample": 0.8,
        },
    },
    {
        "id": "exp33_adaboost",
        "model": "adaboost",
        "calibrate": True,
        "preprocess": "default",
        "min_profit_proba": 0.55,
        "params": {"n_estimators": 250, "learning_rate": 0.6},
    },
    {
        "id": "exp34_gaussian_nb",
        "model": "gaussian_nb",
        "calibrate": True,
        "preprocess": "standard",
        "min_profit_proba": 0.55,
        "params": {"var_smoothing": 1e-8},
    },
    {
        "id": "exp35_lda",
        "model": "lda",
        "calibrate": True,
        "preprocess": "standard",
        "min_profit_proba": 0.55,
        "params": {"solver": "svd"},
    },
    {
        "id": "exp36_lgbm_standard",
        "model": "lightgbm",
        "calibrate": True,
        "preprocess": "standard",
        "min_profit_proba": 0.55,
        "params": dict(_LGBM_BASE),
    },
    {
        "id": "exp37_lgbm_robust",
        "model": "lightgbm",
        "calibrate": True,
        "preprocess": "robust",
        "min_profit_proba": 0.55,
        "params": dict(_LGBM_BASE),
    },
    {
        "id": "exp38_lgbm_quantile",
        "model": "lightgbm",
        "calibrate": True,
        "preprocess": "quantile",
        "min_profit_proba": 0.55,
        "params": dict(_LGBM_BASE),
    },
    {
        "id": "exp39_lgbm_winsor",
        "model": "lightgbm",
        "calibrate": True,
        "preprocess": "winsor",
        "min_profit_proba": 0.55,
        "params": dict(_LGBM_BASE),
    },
    {
        "id": "exp40_lgbm_market_only",
        "model": "lightgbm",
        "calibrate": True,
        "preprocess": "market_only",
        "min_profit_proba": 0.55,
        "params": dict(_LGBM_BASE),
    },
    {
        "id": "exp41_lgbm_num_only",
        "model": "lightgbm",
        "calibrate": True,
        "preprocess": "num_only",
        "min_profit_proba": 0.55,
        "params": dict(_LGBM_BASE),
    },
    {
        "id": "exp42_lgbm_pca24",
        "model": "lightgbm",
        "calibrate": True,
        "preprocess": "pca24",
        "min_profit_proba": 0.55,
        "params": dict(_LGBM_BASE),
    },
    {
        "id": "exp43_lgbm_kbest40",
        "model": "lightgbm",
        "calibrate": True,
        "preprocess": "kbest40",
        "min_profit_proba": 0.55,
        "params": dict(_LGBM_BASE),
    },
    {
        "id": "exp44_lgbm_no_time",
        "model": "lightgbm",
        "calibrate": True,
        "preprocess": "no_time",
        "min_profit_proba": 0.55,
        "params": dict(_LGBM_BASE),
    },
    {
        "id": "exp45_lgbm_sigmoid_cal",
        "model": "lightgbm",
        "calibrate": True,
        "calibrate_method": "sigmoid",
        "preprocess": "default",
        "min_profit_proba": 0.55,
        "params": dict(_LGBM_BASE),
    },
    {
        "id": "exp46_bagging_lgbm",
        "model": "bagging_lgbm",
        "calibrate": True,
        "preprocess": "default",
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 10,
            "max_samples": 0.75,
            "max_features": 0.8,
            "base_n_estimators": 180,
            "base_learning_rate": 0.06,
            "base_max_depth": 6,
            "base_num_leaves": 31,
        },
    },
    {
        "id": "exp47_voting_soft",
        "model": "voting_soft",
        "calibrate": False,
        "preprocess": "default",
        "min_profit_proba": 0.55,
        "params": {},
    },
    {
        "id": "exp48_xgb_robust_gate045",
        "model": "xgboost",
        "calibrate": True,
        "preprocess": "robust",
        "min_profit_proba": 0.45,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 6,
            "min_child_weight": 20,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "scale_pos_weight": 1.5,
        },
    },
    {
        "id": "exp49_hist_gbm_quantile",
        "model": "hist_gbm",
        "calibrate": True,
        "preprocess": "quantile",
        "min_profit_proba": 0.50,
        "params": {
            "max_iter": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "min_samples_leaf": 25,
            "class_weight": "balanced",
        },
    },
    {
        "id": "exp50_lgbm_gate040_winsor",
        "model": "lightgbm",
        "calibrate": True,
        "preprocess": "winsor",
        "min_profit_proba": 0.40,
        "params": dict(_LGBM_BASE),
    },
]

EXPERIMENT_BATCHES = {
    1: EXPERIMENTS_BATCH1,
    2: EXPERIMENTS_BATCH2,
    3: EXPERIMENTS_BATCH3,
}


def all_experiments() -> list[dict[str, Any]]:
    return list(EXPERIMENTS_BATCH1) + list(EXPERIMENTS_BATCH2) + list(EXPERIMENTS_BATCH3)


def resolve_experiments(batch: int) -> list[dict[str, Any]]:
    if batch == 0:
        return all_experiments()
    return list(EXPERIMENT_BATCHES[batch])


def load_top30_scenarios() -> list[str]:
    pack = ROOT / "simulation/config/prod_top30_pack.json"
    data = json.loads(pack.read_text(encoding="utf-8"))
    return [s["scenario_id"] for s in (data.get("strategies") or []) if s.get("scenario_id")]


def ranking_rows(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "rank": i + 1,
            "id": r.get("id"),
            "score_ml_test_pnl": r.get("score"),
            "auc": (r.get("classifier") or {}).get("roc_auc"),
            "profit_recall": (r.get("classifier") or {}).get("profit_recall"),
            "keep_rate": (r.get("gate") or {}).get("keep_rate"),
            "kept": (r.get("test_ml") or {}).get("n"),
            "preprocess": (r.get("spec") or {}).get("preprocess"),
            "error": r.get("error"),
        }
        for i, r in enumerate(ranked)
    ]


def run_experiments_on_trades(
    experiments: list[dict[str, Any]],
    train_tr: list[dict[str, Any]],
    test_tr: list[dict[str, Any]],
    train_df: Any,
    test_df: Any,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for spec in experiments:
        try:
            results.append(run_experiment(spec, train_tr, test_tr, train_df, test_df))
        except Exception as exc:
            print(f"FAILED {spec['id']}: {exc}", flush=True)
            results.append({"id": spec["id"], "error": str(exc), "score": -1e18})
    return results


def build_split_frames(
    train_tr: list[dict[str, Any]],
    test_tr: list[dict[str, Any]],
    store: Any,
    cut_ms: int,
) -> tuple[Any, Any]:
    df = build_dataframe(train_tr + test_tr, store)
    train_df = df[df["open_ms"] < cut_ms].copy()
    test_df = df[df["open_ms"] >= cut_ms].copy()
    if len(test_df) < 10:
        train_ms = {int((t.get("trade") or {}).get("open_ms") or 0) for t in train_tr}
        test_ms = {int((t.get("trade") or {}).get("open_ms") or 0) for t in test_tr}
        train_df = df[df["open_ms"].isin(train_ms)].copy()
        test_df = df[df["open_ms"].isin(test_ms)].copy()
    return train_df, test_df


def run_per_scenario(
    *,
    scenarios: list[str],
    experiments: list[dict[str, Any]],
    cache_dir: Path,
    out_dir: Path,
    cut_ms: int,
    cut_date: str,
    train_range: str,
    test_range: str,
    force: bool = False,
    min_train: int = 40,
    min_test: int = 10,
    merge_existing: bool = True,
) -> dict[str, Any]:
    store = make_market_store(ROOT)
    per_dir = out_dir / "per_scenario"
    per_dir.mkdir(parents=True, exist_ok=True)
    summary_rows: list[dict[str, Any]] = []
    exp_ids = {e["id"] for e in experiments}

    for i, sid in enumerate(scenarios, 1):
        sid_path = per_dir / f"{sid}.json"
        prev_results: list[dict[str, Any]] = []
        if sid_path.is_file() and not force:
            prev = json.loads(sid_path.read_text(encoding="utf-8"))
            prev_results = list(prev.get("experiments") or [])
            done = {r.get("id") for r in prev_results if not r.get("error")}
            if exp_ids.issubset(done):
                print(f"[{i}/{len(scenarios)}] skip {sid} (report exists)", flush=True)
                ranked_prev = sorted(
                    prev_results, key=lambda r: float(r.get("score") or -1e18), reverse=True
                )
                w = prev.get("winner") or (ranked_prev[0].get("id") if ranked_prev else None)
                summary_rows.append(
                    {
                        "scenario_id": sid,
                        "winner": w,
                        "score_ml_test_pnl": (ranked_prev[0].get("score") if ranked_prev else None),
                        "n_train": prev.get("n_train_trades"),
                        "n_test": prev.get("n_test_trades"),
                        "skipped": True,
                    }
                )
                continue

        train_tr = load_cached(cache_path(cache_dir, sid, "train")) or []
        test_tr = load_cached(cache_path(cache_dir, sid, "test")) or []
        print(
            f"\n[{i}/{len(scenarios)}] {sid}: train={len(train_tr)} test={len(test_tr)} "
            f"· {len(experiments)} exps",
            flush=True,
        )
        if len(train_tr) < min_train or len(test_tr) < min_test:
            row = {
                "scenario_id": sid,
                "skipped": True,
                "reason": f"need train>={min_train} test>={min_test}",
                "n_train": len(train_tr),
                "n_test": len(test_tr),
                "winner": None,
                "score_ml_test_pnl": None,
            }
            summary_rows.append(row)
            sid_path.write_text(json.dumps(row, indent=2, ensure_ascii=False), encoding="utf-8")
            continue

        train_df, test_df = build_split_frames(train_tr, test_tr, store, cut_ms)
        if len(train_df) < min_train or len(test_df) < min_test:
            row = {
                "scenario_id": sid,
                "skipped": True,
                "reason": "dataframe too small after features",
                "n_train": len(train_tr),
                "n_test": len(test_tr),
                "winner": None,
                "score_ml_test_pnl": None,
            }
            summary_rows.append(row)
            sid_path.write_text(json.dumps(row, indent=2, ensure_ascii=False), encoding="utf-8")
            continue

        # Only train missing experiment ids when merging into existing reports.
        to_run = experiments
        kept_prev: list[dict[str, Any]] = []
        if merge_existing and prev_results and not force:
            done_ok = {r.get("id") for r in prev_results if not r.get("error")}
            to_run = [e for e in experiments if e["id"] not in done_ok]
            kept_prev = [r for r in prev_results if r.get("id") not in exp_ids or not r.get("error")]
            # Prefer previous non-error results for ids outside this batch.
            kept_prev = [r for r in prev_results if r.get("id") not in {e["id"] for e in to_run}]
            print(f"  merge: keep {len(kept_prev)} prev, run {len(to_run)} new", flush=True)

        new_results = run_experiments_on_trades(to_run, train_tr, test_tr, train_df, test_df)
        by_id: dict[str, dict[str, Any]] = {}
        for r in kept_prev:
            if r.get("id"):
                by_id[str(r["id"])] = r
        for r in new_results:
            if r.get("id"):
                by_id[str(r["id"])] = r
        # Ensure all requested batch results are present (even errors).
        for e in experiments:
            if e["id"] not in by_id:
                # shouldn't happen
                pass
        results = list(by_id.values())
        ranked = sorted(results, key=lambda r: float(r.get("score") or -1e18), reverse=True)
        report = {
            "scenario_id": sid,
            "train_range": train_range,
            "test_range": test_range,
            "cut_date": cut_date,
            "cut_ms": cut_ms,
            "n_train_trades": len(train_tr),
            "n_test_trades": len(test_tr),
            "n_experiments": len(results),
            "experiments": results,
            "ranking": ranking_rows(ranked),
            "winner": ranked[0].get("id") if ranked else None,
            "generated_at": datetime.now(tz=UTC).isoformat(),
        }
        sid_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        best = ranked[0] if ranked else {}
        print(
            f"  winner={best.get('id')} ml_pnl={(best.get('test_ml') or {}).get('pnl')}",
            flush=True,
        )
        summary_rows.append(
            {
                "scenario_id": sid,
                "winner": report["winner"],
                "score_ml_test_pnl": best.get("score"),
                "auc": (best.get("classifier") or {}).get("roc_auc"),
                "profit_recall": (best.get("classifier") or {}).get("profit_recall"),
                "keep_rate": (best.get("gate") or {}).get("keep_rate"),
                "kept": (best.get("test_ml") or {}).get("n"),
                "n_train": len(train_tr),
                "n_test": len(test_tr),
                "skipped": False,
                "report": str(sid_path),
            }
        )

    ranked_summary = sorted(
        [r for r in summary_rows if r.get("score_ml_test_pnl") is not None],
        key=lambda r: float(r.get("score_ml_test_pnl") or -1e18),
        reverse=True,
    )
    summary = {
        "mode": "per_scenario",
        "train_range": train_range,
        "test_range": test_range,
        "cut_date": cut_date,
        "n_scenarios": len(scenarios),
        "n_experiments": len(experiments),
        "experiment_ids": [e["id"] for e in experiments],
        "cache_dir": str(cache_dir),
        "cache_preserved": True,
        "scenarios": summary_rows,
        "ranking_by_best_ml_pnl": [
            {
                "rank": i + 1,
                "scenario_id": r["scenario_id"],
                "winner_exp": r.get("winner"),
                "score_ml_test_pnl": r.get("score_ml_test_pnl"),
                "auc": r.get("auc"),
                "kept": r.get("kept"),
            }
            for i, r in enumerate(ranked_summary)
        ],
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }
    summary_path = out_dir / "report_per_scenario.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n========== PER-SCENARIO WINNERS (best ML PnL) ==========", flush=True)
    for i, r in enumerate(ranked_summary[:20], 1):
        print(
            f"{i}. {r['scenario_id']}: exp={r.get('winner')} "
            f"ml_pnl={r.get('score_ml_test_pnl')} kept={r.get('kept')}",
            flush=True,
        )
    print(f"\nSaved: {summary_path}", flush=True)
    return summary


def cache_path(cache_dir: Path, sid: str, split: str) -> Path:
    return cache_dir / f"{sid}__{split}.json"


def load_cached(path: Path) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_cached(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def collect_all(
    scenarios: list[str],
    pairs: list[str],
    *,
    train_range: str,
    test_range: str,
    cache_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    datadir = ROOT / "simulation/data/ctengine"
    mgr = BotSessionManager(ROOT)
    try:
        mgr._get_ml_gate().set_enabled(False)
    except Exception:
        pass
    mgr.scan_schedule = {"rolling_whitelist": False}

    train_all: list[dict[str, Any]] = []
    test_all: list[dict[str, Any]] = []
    for i, sid in enumerate(scenarios, 1):
        tr_path = cache_path(cache_dir, sid, "train")
        te_path = cache_path(cache_dir, sid, "test")
        train_tr = load_cached(tr_path)
        test_tr = load_cached(te_path)
        if train_tr is None or test_tr is None:
            print(f"\n[{i}/{len(scenarios)}] backtest {sid} · {len(pairs)} pairs", flush=True)
            train_tr, test_tr = collect_train_test_trades(
                mgr,
                sid,
                pairs,
                datadir,
                train_range=train_range,
                test_range=test_range,
            )
            save_cached(tr_path, train_tr)
            save_cached(te_path, test_tr)
        else:
            print(
                f"[{i}/{len(scenarios)}] cache hit {sid}: "
                f"train={len(train_tr)} test={len(test_tr)}",
                flush=True,
            )
        train_all.extend(train_tr)
        test_all.extend(test_tr)
    return train_all, test_all


def run_experiment(
    spec: dict[str, Any],
    train_tr: list[dict[str, Any]],
    test_tr: list[dict[str, Any]],
    train_df: Any,
    test_df: Any,
) -> dict[str, Any]:
    print(f"\n=== {spec['id']} ===", flush=True)
    pipe = make_exp_pipeline(spec)
    clf_metrics = evaluate_pipeline(pipe, train_df, test_df)
    kept, gate = apply_ml_gate(
        pipe,
        test_df,
        test_tr,
        min_profit_proba=float(spec["min_profit_proba"]),
    )
    raw = summarize(test_tr)
    ml = summarize(kept)
    out = {
        "id": spec["id"],
        "spec": {
            "model": spec["model"],
            "calibrate": spec.get("calibrate", True),
            "calibrate_method": spec.get("calibrate_method"),
            "preprocess": spec.get("preprocess") or "default",
            "min_profit_proba": spec["min_profit_proba"],
            "params": spec.get("params") or {},
        },
        "n_train": len(train_tr),
        "n_test": len(test_tr),
        "classifier": clf_metrics,
        "gate": gate,
        "test_raw": raw,
        "test_ml": ml,
        "score": ml["pnl"],
    }
    print(
        f"  auc={clf_metrics.get('roc_auc')} profit_rec={clf_metrics.get('profit_recall')} "
        f"keep={gate.get('keep_rate')} ml_pnl={ml['pnl']} (kept={ml['n']})",
        flush=True,
    )
    return out


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="ML param experiments, April cut")
    ap.add_argument("--train-range", default=DEFAULT_TRAIN)
    ap.add_argument("--test-range", default=DEFAULT_TEST)
    ap.add_argument("--pairs", default="all", help="all|prod200|pool")
    ap.add_argument("--max-pairs", type=int, default=0, help="0 = no limit")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument(
        "--cache-dir",
        default=str(ROOT / "simulation/results/ml_param_experiments/trade_cache"),
    )
    ap.add_argument(
        "--scenarios",
        default="all",
        help="all | top30 | comma ids",
    )
    ap.add_argument("--skip-collect", action="store_true", help="use cache only (never deletes cache)")
    ap.add_argument(
        "--batch",
        type=int,
        default=1,
        choices=sorted(EXPERIMENT_BATCHES),
        help="experiment batch: 1=exp01-15, 2=exp16-30, 3=exp31-50 (new models+preprocess)",
    )
    ap.add_argument(
        "--report-name",
        default="",
        help="report filename (default report_batch{N}.json)",
    )
    args = ap.parse_args()

    experiments = list(EXPERIMENT_BATCHES[args.batch])
    report_name = args.report_name.strip() or f"report_batch{args.batch}.json"

    scen_arg = args.scenarios.strip().lower()
    if scen_arg == "all":
        scenarios = list(ALL_SCENARIOS)
    elif scen_arg == "top30":
        scenarios = load_top30_scenarios()
    else:
        scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]

    pairs = pairs_from_source(ROOT, args.pairs, min_start="2025-01-01")
    pairs = [p for p in pairs if not p.startswith("XRP/")]
    if args.max_pairs and args.max_pairs > 0:
        pairs = pairs[: args.max_pairs]

    te_start, _ = timerange_to_ms(args.test_range)
    cut_date = datetime.fromtimestamp(te_start / 1000, tz=UTC).strftime("%Y-%m-%d")
    out_dir = Path(args.out_dir)
    cache_dir = Path(args.cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # NOTE: trade_cache is never deleted by this script (resume / batch2 reuse).

    print(
        f"=== ML PARAM EXPERIMENTS batch={args.batch} · {len(experiments)} configs · "
        f"{len(scenarios)} scenarios · {len(pairs)} pairs · "
        f"train={args.train_range} test={args.test_range} cut={cut_date} ===",
        flush=True,
    )

    if args.skip_collect:
        train_all: list[dict[str, Any]] = []
        test_all: list[dict[str, Any]] = []
        for sid in scenarios:
            tr = load_cached(cache_path(cache_dir, sid, "train")) or []
            te = load_cached(cache_path(cache_dir, sid, "test")) or []
            train_all.extend(tr)
            test_all.extend(te)
            print(f"cache {sid}: train={len(tr)} test={len(te)}", flush=True)
    else:
        train_all, test_all = collect_all(
            scenarios,
            pairs,
            train_range=args.train_range,
            test_range=args.test_range,
            cache_dir=cache_dir,
        )

    print(
        f"\n=== dataset train={len(train_all)} test={len(test_all)} ===",
        flush=True,
    )
    if len(train_all) < 100 or len(test_all) < 50:
        print("ERROR: not enough trades", flush=True)
        return 1

    store = make_market_store(ROOT)
    print("building feature frames…", flush=True)
    df = build_dataframe(train_all + test_all, store)
    train_df = df[df["open_ms"] < te_start].copy()
    test_df = df[df["open_ms"] >= te_start].copy()
    if len(test_df) < 50:
        train_ms = {int((t.get("trade") or {}).get("open_ms") or 0) for t in train_all}
        test_ms = {int((t.get("trade") or {}).get("open_ms") or 0) for t in test_all}
        train_df = df[df["open_ms"].isin(train_ms)].copy()
        test_df = df[df["open_ms"].isin(test_ms)].copy()
    print(
        f"dataframe train={len(train_df)} test={len(test_df)} "
        f"features={len(feature_columns())}",
        flush=True,
    )

    results: list[dict[str, Any]] = []
    for spec in experiments:
        try:
            results.append(run_experiment(spec, train_all, test_all, train_df, test_df))
        except Exception as exc:
            print(f"FAILED {spec['id']}: {exc}", flush=True)
            results.append({"id": spec["id"], "error": str(exc), "score": -1e18})

    ranked = sorted(results, key=lambda r: float(r.get("score") or -1e18), reverse=True)
    report = {
        "batch": args.batch,
        "train_range": args.train_range,
        "test_range": args.test_range,
        "cut_date": cut_date,
        "cut_ms": te_start,
        "pairs_source": args.pairs,
        "n_pairs": len(pairs),
        "pairs": pairs,
        "n_scenarios": len(scenarios),
        "scenarios": scenarios,
        "n_train_trades": len(train_all),
        "n_test_trades": len(test_all),
        "features": feature_columns(),
        "cache_dir": str(cache_dir),
        "cache_preserved": True,
        "experiments": results,
        "ranking": [
            {
                "rank": i + 1,
                "id": r.get("id"),
                "score_ml_test_pnl": r.get("score"),
                "auc": (r.get("classifier") or {}).get("roc_auc"),
                "profit_recall": (r.get("classifier") or {}).get("profit_recall"),
                "keep_rate": (r.get("gate") or {}).get("keep_rate"),
                "kept": (r.get("test_ml") or {}).get("n"),
                "error": r.get("error"),
            }
            for i, r in enumerate(ranked)
        ],
        "winner": ranked[0].get("id") if ranked else None,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "note": "Original MARKET_FEATURES only; pooled trades; trade_cache never deleted",
    }
    out_path = out_dir / report_name
    # also keep legacy name for batch1
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.batch == 1:
        (out_dir / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    print("\n========== RANKING (ML-gated test PnL) ==========", flush=True)
    for i, r in enumerate(ranked, 1):
        if r.get("error"):
            print(f"{i}. {r['id']}: ERROR {r['error']}", flush=True)
            continue
        ml = r.get("test_ml") or {}
        clf = r.get("classifier") or {}
        print(
            f"{i}. {r['id']}: ml_pnl={ml.get('pnl')} kept={ml.get('n')} "
            f"auc={clf.get('roc_auc')} profit_rec={clf.get('profit_recall')} "
            f"keep={((r.get('gate') or {}).get('keep_rate'))}",
            flush=True,
        )
    print(f"\nWinner: {report['winner']}", flush=True)
    print(f"Saved: {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

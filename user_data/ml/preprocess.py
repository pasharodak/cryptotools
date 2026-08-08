"""Sklearn preprocess helper — same class path as sim models expect on unpickle."""
from __future__ import annotations

import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.compose import ColumnTransformer


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

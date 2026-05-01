"""Capping de outliers (sklearn-compat) con metodo configurable.

Soporta dos metodos:
    - 'iqr'        : Q1 - k*IQR  /  Q3 + k*IQR     (k = `factor`)
    - 'percentile' : limites en (lower_q, upper_q) percentiles

`factor` queda como hiperparametro tuneable desde Optuna.
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin

from src.config import NUMERIC_FEATURES
from src.step_02_clean._helpers import resolve_cols

_VALID_METHODS = {"iqr", "percentile"}


class OutlierCapper(BaseEstimator, TransformerMixin):
    """Recorta valores extremos columna a columna sobre las numericas.

    Parametros
    ----------
    factor : float
        Para method='iqr': k del rango intercuartil (1.5 estandar, 3 conservador).
        Ignorado en 'percentile'.
    method : {'iqr', 'percentile'}
        Estrategia de capping.
    lower_q, upper_q : float
        Percentiles para method='percentile' (default 0.01 / 0.99).
    numeric_cols : list[str] | None
        Columnas a procesar; si None usa `config.NUMERIC_FEATURES`.
    """

    def __init__(
        self,
        factor: float = 3.0,
        method: str = "iqr",
        lower_q: float = 0.01,
        upper_q: float = 0.99,
        numeric_cols: Optional[List[str]] = None,
    ):
        self.factor = factor
        self.method = method
        self.lower_q = lower_q
        self.upper_q = upper_q
        self.numeric_cols = numeric_cols

    def _resolve_cols(self, X: pd.DataFrame) -> List[str]:
        return resolve_cols(X, self.numeric_cols, NUMERIC_FEATURES, "OutlierCapper")

    def fit(self, X: pd.DataFrame, y=None) -> "OutlierCapper":
        if self.method not in _VALID_METHODS:
            raise ValueError(f"method debe ser uno de {_VALID_METHODS}, dado '{self.method}'")

        cols = self._resolve_cols(X)
        self.numeric_cols_ = cols

        if self.method == "iqr":
            q1 = X[cols].quantile(0.25)
            q3 = X[cols].quantile(0.75)
            iqr = q3 - q1
            lower = q1 - self.factor * iqr
            upper = q3 + self.factor * iqr
        else:  # percentile
            lower = X[cols].quantile(self.lower_q)
            upper = X[cols].quantile(self.upper_q)

        self.lower_ = lower.to_dict()
        self.upper_ = upper.to_dict()
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        X = X.copy()
        for c in self.numeric_cols_:
            X[c] = np.clip(X[c].astype(float), self.lower_[c], self.upper_[c])
        return X

    def get_feature_names_out(self, input_features=None):
        return list(input_features) if input_features is not None else None

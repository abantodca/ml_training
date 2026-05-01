"""Imputacion de valores faltantes (sklearn-compat).

Estrategia robusta:
    - Numericas con < `median_threshold` de missing -> KNNImputer.
    - Si no quedan columnas con datos suficientes para entrenar el KNN
      (todo missing en alguna fila) cae a SimpleImputer median.
    - Permite tunear `n_neighbors` desde Optuna via set_params.
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.impute import KNNImputer, SimpleImputer

from src.config import NUMERIC_FEATURES
from src.step_02_clean._helpers import resolve_cols


class CustomKNNImputer(BaseEstimator, TransformerMixin):
    """Wrapper sklearn-compat sobre `sklearn.impute.KNNImputer`.

    Imputa solo las columnas numericas indicadas y conserva las demas
    intactas (incluidas categoricas y la columna de fecha), de modo que
    pueda encadenarse con transformadores posteriores.

    Parametros
    ----------
    n_neighbors : int
        Vecinos del KNN. Tuneable.
    weights : {'uniform', 'distance'}
        Ponderacion de vecinos.
    add_median_fallback : bool
        Si True, primero rellena con la mediana columnas con missing
        ratio > `fallback_threshold` (KNN no aporta cuando hay > 50%
        missing) y luego corre KNN sobre el resto.
    fallback_threshold : float
        Ratio de missing por encima del cual se usa mediana antes que KNN.
    numeric_cols : list[str] | None
        Columnas a imputar; si None usa `config.NUMERIC_FEATURES`.
    """

    def __init__(
        self,
        n_neighbors: int = 10,
        weights: str = "distance",
        add_median_fallback: bool = True,
        fallback_threshold: float = 0.30,
        numeric_cols: Optional[List[str]] = None,
    ):
        self.n_neighbors = n_neighbors
        self.weights = weights
        self.add_median_fallback = add_median_fallback
        self.fallback_threshold = fallback_threshold
        self.numeric_cols = numeric_cols

    def _resolve_cols(self, X: pd.DataFrame) -> List[str]:
        return resolve_cols(X, self.numeric_cols, NUMERIC_FEATURES, "CustomKNNImputer")

    def fit(self, X: pd.DataFrame, y=None) -> "CustomKNNImputer":
        cols = self._resolve_cols(X)
        self.numeric_cols_ = cols

        miss_ratio = X[cols].isna().mean()
        if self.add_median_fallback:
            self.median_cols_ = miss_ratio[miss_ratio > self.fallback_threshold].index.tolist()
        else:
            self.median_cols_ = []
        self.knn_cols_ = [c for c in cols if c not in self.median_cols_]

        if self.median_cols_:
            self.median_imputer_ = SimpleImputer(strategy="median")
            self.median_imputer_.fit(X[self.median_cols_])

        if self.knn_cols_:
            self.knn_imputer_ = KNNImputer(
                n_neighbors=int(self.n_neighbors),
                weights=self.weights,
            )
            # Entrenamos KNN sobre TODAS las numericas para mejor distancia,
            # pero solo aplicamos transform a las knn_cols_.
            self._knn_fit_cols_ = cols
            X_knn = X[cols].copy()
            if self.median_cols_:
                X_knn[self.median_cols_] = self.median_imputer_.transform(
                    X[self.median_cols_]
                )
            self.knn_imputer_.fit(X_knn)

        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        X = X.copy()
        cols = self.numeric_cols_

        if self.median_cols_:
            X[self.median_cols_] = self.median_imputer_.transform(X[self.median_cols_])

        if self.knn_cols_:
            X_knn_in = X[cols].copy()
            X_knn_out = self.knn_imputer_.transform(X_knn_in)
            # Solo escribimos las columnas que delegamos al KNN
            knn_idx = [cols.index(c) for c in self.knn_cols_]
            for i, col in zip(knn_idx, self.knn_cols_):
                X[col] = X_knn_out[:, i]

        # Garantia: no debe quedar NaN en numericas
        for c in cols:
            if X[c].isna().any():
                X[c] = X[c].fillna(X[c].median())

        return X

    def get_feature_names_out(self, input_features=None):
        return list(input_features) if input_features is not None else None

"""Indicadores binarios de missingness (sklearn-compat).

Crea columnas `<col>__MISS` (1 si el valor original era NaN, 0 en caso
contrario) ANTES de que el imputer rellene los huecos. La ausencia es
informativa: en el dataset POP, las filas con P/BAYA NaN tenian MAPE
17.3% vs 15.6% en filas observadas, asi que esa senal vale la pena
exponerla al modelo.
"""
from __future__ import annotations

from typing import List, Optional

import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin

from src.step_02_clean._helpers import resolve_cols

# Default: solo columnas con missing significativo en EDA. Tuneable via
# constructor si se entrenan otras variedades con patrones distintos.
DEFAULT_FLAG_COLS: List[str] = ["%INDUS", "P/BAYA"]
SUFFIX = "__MISS"


class MissingFlagger(BaseEstimator, TransformerMixin):
    """Agrega `<col>__MISS` para columnas con missing.

    Va al INICIO del preprocesador (antes del imputer), de modo que el
    modelo reciba la flag y el valor imputado.
    """

    def __init__(self, cols: Optional[List[str]] = None):
        self.cols = cols

    def _resolve(self, X: pd.DataFrame) -> List[str]:
        return resolve_cols(
            X, self.cols, DEFAULT_FLAG_COLS, "MissingFlagger", require_all=False,
        )

    def fit(self, X: pd.DataFrame, y=None) -> "MissingFlagger":
        self.cols_ = self._resolve(X)
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        X = X.copy()
        for c in self.cols_:
            X[f"{c}{SUFFIX}"] = X[c].isna().astype(int)
        return X

    def get_feature_names_out(self, input_features=None):
        base = list(input_features) if input_features is not None else []
        return base + [f"{c}{SUFFIX}" for c in getattr(self, "cols_", [])]

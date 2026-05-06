"""Capping de outliers (sklearn-compat) con metodo configurable.

Soporta dos metodos:
    - 'iqr'        : Q1 - k*IQR  /  Q3 + k*IQR     (k = `factor`)
    - 'percentile' : limites en (lower_q, upper_q) percentiles

`factor` queda como hiperparametro tuneable desde Optuna.

Granularidad opcional por GRUPO (e.g. FUNDO):
    Si `group_col` se setea, aprende limites SEPARADOS por grupo. Util cuando
    fundos / segmentos tienen escalas estructuralmente distintas (un fundo
    rinde 8-15 KG/HA, otro 2-5 KG/HA): el cap global cortaria reales del
    fundo bueno o no tocaria outliers reales del fundo bajo.

    Grupos con n < `min_group_size` caen al cap GLOBAL (insuficiente data
    para estimar IQR confiable). Grupos no vistos en fit (categoria nueva
    en transform) tambien caen al global.
"""
from __future__ import annotations

from typing import Dict, List, Optional

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
    group_col : str | None
        Si se provee (e.g. 'FUNDO'), aprende limites por grupo. Grupos con
        n < min_group_size caen al limite global. Default None = comportamiento
        legacy (cap unico global).
    min_group_size : int
        Tamano minimo de grupo para usar limites por-grupo. Si n<umbral, ese
        grupo cae al limite global. 30 es default empirico: bajo de 30 obs
        el IQR es muy ruidoso.
    """

    def __init__(
        self,
        factor: float = 3.0,
        method: str = "iqr",
        lower_q: float = 0.01,
        upper_q: float = 0.99,
        numeric_cols: Optional[List[str]] = None,
        group_col: Optional[str] = None,
        min_group_size: int = 30,
    ):
        self.factor = factor
        self.method = method
        self.lower_q = lower_q
        self.upper_q = upper_q
        self.numeric_cols = numeric_cols
        self.group_col = group_col
        self.min_group_size = min_group_size

    def _resolve_cols(self, X: pd.DataFrame) -> List[str]:
        return resolve_cols(X, self.numeric_cols, NUMERIC_FEATURES, "OutlierCapper")

    def _compute_bounds(self, X: pd.DataFrame, cols: List[str]) -> Dict[str, float]:
        """IQR/percentile bounds sobre un subset de filas. Devuelve dict
        plano {col: {lower, upper}} para guardar en self."""
        if self.method == "iqr":
            q1 = X[cols].quantile(0.25)
            q3 = X[cols].quantile(0.75)
            iqr = q3 - q1
            lower = q1 - self.factor * iqr
            upper = q3 + self.factor * iqr
        else:  # percentile
            lower = X[cols].quantile(self.lower_q)
            upper = X[cols].quantile(self.upper_q)
        return {"lower": lower.to_dict(), "upper": upper.to_dict()}

    def fit(self, X: pd.DataFrame, y=None) -> "OutlierCapper":
        if self.method not in _VALID_METHODS:
            raise ValueError(
                f"method debe ser uno de {_VALID_METHODS}, dado '{self.method}'"
            )

        cols = self._resolve_cols(X)
        self.numeric_cols_ = cols

        # Limites GLOBALES (siempre se calculan, sirven de fallback aun cuando
        # group_col esta seteado). Bound legacy.
        global_bounds = self._compute_bounds(X, cols)
        self.lower_ = global_bounds["lower"]
        self.upper_ = global_bounds["upper"]

        # Limites POR GRUPO si group_col aplica.
        self.group_col_: Optional[str] = (
            self.group_col if self.group_col and self.group_col in X.columns else None
        )
        self.group_lower_: Dict[str, Dict[str, float]] = {}
        self.group_upper_: Dict[str, Dict[str, float]] = {}
        self.small_groups_: List[str] = []  # informativo, para debug

        if self.group_col_ is not None:
            for grp_val, X_grp in X.groupby(self.group_col_, sort=False):
                if len(X_grp) < self.min_group_size:
                    # Marca para fallback global; no calculamos bounds.
                    self.small_groups_.append(str(grp_val))
                    continue
                bnd = self._compute_bounds(X_grp, cols)
                self.group_lower_[str(grp_val)] = bnd["lower"]
                self.group_upper_[str(grp_val)] = bnd["upper"]
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        X = X.copy()

        if self.group_col_ is None or not self.group_lower_:
            # Path legacy (sin grupos o todos los grupos eran chicos).
            for c in self.numeric_cols_:
                X[c] = np.clip(X[c].astype(float), self.lower_[c], self.upper_[c])
            return X

        # Path por grupos: itera por grupo (cada uno O(1) lookups en dicts) y
        # aplica np.clip vectorizado a TODAS las filas de ese grupo en una
        # sola operacion. O(n_groups * n_cols) operaciones bulk numpy en vez
        # del lambda map.row-a-row anterior, que era ~100x mas lento.
        # Grupos no vistos en fit caen al cap GLOBAL.
        group_keys = X[self.group_col_].astype(str)
        for grp_val, grp_idx in group_keys.groupby(group_keys, sort=False).groups.items():
            grp_lower = self.group_lower_.get(str(grp_val), self.lower_)
            grp_upper = self.group_upper_.get(str(grp_val), self.upper_)
            for c in self.numeric_cols_:
                X.loc[grp_idx, c] = np.clip(
                    X.loc[grp_idx, c].astype(float),
                    grp_lower[c],
                    grp_upper[c],
                )
        return X

    def get_feature_names_out(self, input_features=None):
        return list(input_features) if input_features is not None else None

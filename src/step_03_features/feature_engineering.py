"""Ingenieria de variables (sklearn-compat).

`FeatureGenerator` hace TODO el trabajo de transformar el DataFrame raw
(numericas + categoricas + columna fecha) en una matriz numerica lista
para el modelo:

    1. Deriva features ciclicas y `ANIO` desde la columna de fecha.
    2. Calcula ratios estructurales intra-fila (sin tiempo, sin target):
       KG_TOTAL, INDUS_KG_HA, KG_PER_BAYA, KG_HA_PER_DPC.
    3. Aplica one-hot encoding a las categoricas, memorizando categorias
       en `fit` para que `transform` produzca SIEMPRE las mismas columnas.
    4. Descarta la columna de fecha original.
    5. Devuelve un DataFrame con orden de columnas estable.

Codificacion ciclica de tiempo:
    sin(2*pi * x / period), cos(2*pi * x / period)
para que el modelo perciba que diciembre y enero estan adyacentes.

Ratios estructurales: combinaciones intra-fila que los lag features (que
operan a nivel grupo+temporal) no capturan. Son determinísticas por fila
y NO usan target ni H-EF, asi que no hay riesgo de leakage. Las divisiones
usan np.where(den > 0, num/den, NaN) y dejan que XGB/LGB redirijan los NaN
a su rama default por loss (tratamiento nativo de NaN en gradient boosting).
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin

from src.config import CATEGORICAL_FEATURES, DATE_COLUMN


class FeatureGenerator(BaseEstimator, TransformerMixin):
    """One-hot + ciclicas de fecha.

    Parametros
    ----------
    categorical_cols : list[str] | None
        Categoricas a codificar. None = `config.CATEGORICAL_FEATURES`.
    date_col : str | None
        Nombre de la columna de fecha. None = `config.DATE_COLUMN`.
        Si la columna no existe, no se generan derivadas temporales.
    add_year : bool
        Agrega columna `ANIO` (entero) para capturar deriva temporal.
    """

    def __init__(
        self,
        categorical_cols: Optional[List[str]] = None,
        date_col: Optional[str] = None,
        add_year: bool = True,
    ):
        self.categorical_cols = categorical_cols
        self.date_col = date_col
        self.add_year = add_year

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _resolve_categorical(self, X: pd.DataFrame) -> List[str]:
        cols = (
            self.categorical_cols
            if self.categorical_cols is not None
            else CATEGORICAL_FEATURES
        )
        missing = [c for c in cols if c not in X.columns]
        if missing:
            raise ValueError(f"FeatureGenerator: categoricas inexistentes: {missing}")
        return list(cols)

    def _resolve_date_col(self, X: pd.DataFrame) -> Optional[str]:
        col = self.date_col if self.date_col is not None else DATE_COLUMN
        return col if col in X.columns else None

    @staticmethod
    def _resolve_structural(X: pd.DataFrame) -> List[str]:
        """Decide que ratios se pueden calcular dado X. Defensivo: si una
        columna raw falta, su ratio simplemente no se genera."""
        names: List[str] = []
        if "KG/HA" in X.columns and "HA" in X.columns:
            names.append("KG_TOTAL")
        if "%INDUS" in X.columns and "KG/HA" in X.columns:
            names.append("INDUS_KG_HA")
        if "KG/HA" in X.columns and "P/BAYA" in X.columns:
            names.append("KG_PER_BAYA")
        if "KG/HA" in X.columns and "DPC" in X.columns:
            names.append("KG_HA_PER_DPC")
        return names

    @staticmethod
    def _structural_ratios(X: pd.DataFrame, names: List[str]) -> pd.DataFrame:
        out = pd.DataFrame(index=X.index)
        if "KG_TOTAL" in names:
            out["KG_TOTAL"] = (X["KG/HA"].astype(float) * X["HA"].astype(float))
        if "INDUS_KG_HA" in names:
            out["INDUS_KG_HA"] = (X["%INDUS"].astype(float) * X["KG/HA"].astype(float))
        if "KG_PER_BAYA" in names:
            den = X["P/BAYA"].astype(float).to_numpy()
            num = X["KG/HA"].astype(float).to_numpy()
            out["KG_PER_BAYA"] = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)
        if "KG_HA_PER_DPC" in names:
            den = X["DPC"].astype(float).to_numpy()
            num = X["KG/HA"].astype(float).to_numpy()
            out["KG_HA_PER_DPC"] = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)
        return out

    @staticmethod
    def _date_features(series: pd.Series, add_year: bool) -> pd.DataFrame:
        s = pd.to_datetime(series, errors="coerce")
        if s.isna().any():
            s = s.fillna(s.dropna().median())

        out = pd.DataFrame(index=series.index)
        month = s.dt.month.astype(int)
        week = s.dt.isocalendar().week.astype(int)

        # MES: armonicos Fourier orden 1-3. El orden 1 modela la onda anual;
        # 2 y 3 capturan asimetria observada (caida ene-abr, pico sep-oct).
        out["MES_SIN"] = np.sin(2 * np.pi * month / 12.0)
        out["MES_COS"] = np.cos(2 * np.pi * month / 12.0)
        out["MES_SIN2"] = np.sin(4 * np.pi * month / 12.0)
        out["MES_COS2"] = np.cos(4 * np.pi * month / 12.0)
        out["MES_SIN3"] = np.sin(6 * np.pi * month / 12.0)
        out["MES_COS3"] = np.cos(6 * np.pi * month / 12.0)

        out["SEMANA_SIN"] = np.sin(2 * np.pi * week / 52.0)
        out["SEMANA_COS"] = np.cos(2 * np.pi * week / 52.0)

        # DIA_SEM_SIN/COS removidas tras auditoria empirica (2026-05-05):
        # corr con KG/JR_H = +0.031 / -0.001 sobre 10073 filas POP. El η²=0.0014
        # original era universal (toda la senal cabia en ruido); con boosted
        # trees max_depth=4 las tomaba como splits aleatorios en folds chicos
        # y inflaba varianza entre trials de Optuna.

        # Temporada agronomica (referencia=TRANSICION: may, nov, ambos 0).
        # ALTA=jun-oct (z>0), BAJA=dic-abr (z<0 por condicion de fruta).
        out["TEMPORADA_ALTA"] = month.isin([6, 7, 8, 9, 10]).astype(int)
        out["TEMPORADA_BAJA"] = month.isin([12, 1, 2, 3, 4]).astype(int)

        if add_year:
            out["ANIO"] = s.dt.year.astype(int)
        return out

    # ------------------------------------------------------------------
    # Sklearn API
    # ------------------------------------------------------------------
    def fit(self, X: pd.DataFrame, y=None) -> "FeatureGenerator":
        cat_cols = self._resolve_categorical(X)
        date_col = self._resolve_date_col(X)

        self.categorical_cols_ = cat_cols
        self.date_col_ = date_col

        # Categorias memorizadas
        self.categories_ = {
            c: sorted(map(str, X[c].dropna().unique().tolist())) for c in cat_cols
        }

        # Columnas que pasan tal cual: numericas (todo lo demas)
        passthrough = [
            c for c in X.columns if c not in cat_cols and c != date_col
        ]
        self.passthrough_cols_ = passthrough

        # Ratios estructurales: cuales se pueden calcular dadas las columnas
        # disponibles. Memorizado para que transform() produzca el mismo
        # set sin re-evaluar.
        self.structural_feature_names_ = self._resolve_structural(X)

        # Derivadas de fecha (DIA_SEM_SIN/COS removidas: corr con target ~0).
        if date_col is not None:
            self.date_feature_names_ = (
                [
                    "MES_SIN", "MES_COS",
                    "MES_SIN2", "MES_COS2",
                    "MES_SIN3", "MES_COS3",
                    "SEMANA_SIN", "SEMANA_COS",
                    "TEMPORADA_ALTA", "TEMPORADA_BAJA",
                ]
                + (["ANIO"] if self.add_year else [])
            )
        else:
            self.date_feature_names_ = []

        # Dummies finales
        dummy_cols = [
            f"{c}__{cat}" for c in cat_cols for cat in self.categories_[c]
        ]
        self.feature_names_out_ = (
            passthrough
            + self.structural_feature_names_
            + self.date_feature_names_
            + dummy_cols
        )
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        X = X.copy().reset_index(drop=True)

        passthrough_part = X[self.passthrough_cols_].copy()
        structural_part = self._structural_ratios(X, self.structural_feature_names_)

        if self.date_col_ is not None:
            date_part = self._date_features(X[self.date_col_], self.add_year)
        else:
            date_part = pd.DataFrame(index=X.index)

        dummy_frames: List[pd.DataFrame] = []
        for c in self.categorical_cols_:
            series = X[c].astype(str)
            dummies = pd.get_dummies(series, prefix=c, prefix_sep="__")
            for cat in self.categories_[c]:
                col_name = f"{c}__{cat}"
                if col_name not in dummies.columns:
                    dummies[col_name] = 0
            keep = [f"{c}__{cat}" for cat in self.categories_[c]]
            dummy_frames.append(dummies[keep].astype(int))

        out = pd.concat(
            [passthrough_part, structural_part, date_part, *dummy_frames],
            axis=1,
        )
        return out.loc[:, self.feature_names_out_]

    def get_feature_names_out(self, input_features=None):
        return list(self.feature_names_out_)

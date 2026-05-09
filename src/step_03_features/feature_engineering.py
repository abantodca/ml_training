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

from src.config import CATEGORICAL_FEATURES, DATE_COLUMN, SKEW_LOG1P_COLS, SKEW_SQRT_COLS


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
    def _resolve_skew_features(X: pd.DataFrame) -> List[str]:
        """Lista los nombres de skew-mitigated features que vamos a generar.

        Para cada col en SKEW_LOG1P_COLS o SKEW_SQRT_COLS que efectivamente
        existe en X, agrega `<col>_LOG1P` o `<col>_SQRT` al feature set.

        EDA POP 2026-05-09: las recomendaciones por variable provienen de
        Box-Cox lambda + |skew| analysis. Ver config.py para criterios.
        """
        names: List[str] = []
        for c in SKEW_LOG1P_COLS:
            if c in X.columns:
                names.append(f"{c}_LOG1P")
        for c in SKEW_SQRT_COLS:
            if c in X.columns:
                names.append(f"{c}_SQRT")
        return names

    @staticmethod
    def _skew_mitigated(X: pd.DataFrame, names: List[str]) -> pd.DataFrame:
        """Aplica log1p / sqrt a las columnas listadas. NaN-safe; valores
        negativos se shiftean (col + |min| + 1) antes de log1p / sqrt para
        evitar -inf y mantener monotonia."""
        out = pd.DataFrame(index=X.index)
        for name in names:
            if name.endswith("_LOG1P"):
                src_col = name[: -len("_LOG1P")]
                v = pd.to_numeric(X[src_col], errors="coerce").astype(float)
                # Shift defensivo si hay negativos (no esperado en POP, pero
                # generaliza a otras variedades): mover a >=0 antes de log1p.
                min_v = v.min(skipna=True)
                if pd.notna(min_v) and min_v < 0:
                    v = v - float(min_v)
                out[name] = np.log1p(v)
            elif name.endswith("_SQRT"):
                src_col = name[: -len("_SQRT")]
                v = pd.to_numeric(X[src_col], errors="coerce").astype(float)
                min_v = v.min(skipna=True)
                if pd.notna(min_v) and min_v < 0:
                    v = v - float(min_v)
                out[name] = np.sqrt(v)
        return out

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
    def _date_features(series: pd.Series, add_year: bool,
                       trend_anchor: pd.Timestamp | None = None) -> pd.DataFrame:
        """Features derivadas de FECHA: ciclicas (Fourier) + tendencia.

        `trend_anchor` (memorized en fit): timestamp inicial del dataset
        de training. En transform usamos el MISMO anchor para que
        `t_index` calculado en inference sea consistente con el del
        training (no leakage de "cuanto tiempo paso entre training y
        inference"). EDA POP 2026-05-09 detecto DW=0.19 + KPSS rechazo
        de estacionariedad: el target tiene tendencia que no fue
        capturada por las ciclicas + lags. Exponer t_index como feature
        permite a los arboles modelar tendencia explicitamente.
        """
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

        # FEATURES DE TENDENCIA (EDA POP 2026-05-09: DW=0.19 + KPSS rechaza)
        # t_index: dias desde el anchor (primer dia del training fold).
        # En inference, anchor es el del fit -> permite extrapolar en forma
        # consistente. Boosted tree puede splittar t_index para capturar
        # tendencia (e.g. "antes de t=400 KG/JR_H promedio era X, despues Y").
        if trend_anchor is not None:
            t_days = (s - trend_anchor).dt.days.astype(float)
            out["t_index_days"] = t_days
            out["t_index_years"] = t_days / 365.25

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

        # Skew-mitigated features (EDA POP 2026-05-09: KG/HA, %INDUS, HA log1p;
        # DPC sqrt por kurt extremo). Memorizado en fit para que transform
        # produzca SIEMPRE el mismo set. Anchor temporal para t_index_*
        # se memoriza tambien (primer dia visto en fit).
        self.skew_feature_names_ = self._resolve_skew_features(X)

        # Trend anchor: primer dia visto durante el fit. Se usa en transform
        # para que t_index_days/years sean consistentes entre training y
        # inference (relativos al mismo origen). Si date_col no esta, anchor
        # queda None y t_index_* no se generan.
        self.trend_anchor_ = None
        if date_col is not None:
            dates = pd.to_datetime(X[date_col], errors="coerce").dropna()
            if not dates.empty:
                self.trend_anchor_ = dates.min()

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
                + (["t_index_days", "t_index_years"] if self.trend_anchor_ is not None else [])
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
            + self.skew_feature_names_
            + self.date_feature_names_
            + dummy_cols
        )
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        X = X.copy().reset_index(drop=True)

        passthrough_part = X[self.passthrough_cols_].copy()
        structural_part = self._structural_ratios(X, self.structural_feature_names_)
        skew_part = self._skew_mitigated(X, getattr(self, "skew_feature_names_", []))

        if self.date_col_ is not None:
            date_part = self._date_features(
                X[self.date_col_], self.add_year,
                trend_anchor=getattr(self, "trend_anchor_", None),
            )
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
            [passthrough_part, structural_part, skew_part, date_part, *dummy_frames],
            axis=1,
        )
        return out.loc[:, self.feature_names_out_]

    def get_feature_names_out(self, input_features=None):
        return list(self.feature_names_out_)

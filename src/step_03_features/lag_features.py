"""Features lag/agregadas por FUNDO+FORMATO, FUNDO y FORMATO usando historial.

Calcula medianas rolling de KG/JR_H y KG/HA por (FUNDO, FORMATO) y
tambien por FUNDO solo y FORMATO solo (mayor densidad para grupos
chicos), en ventanas de N OBSERVACIONES anteriores ordenadas por
FECHA. `shift(1)` excluye la fila actual.

Tambien agrega ratios "este dia vs su lag":
    KG_HA_ratio_30 = KG/HA actual / KG_HA_lag_FF_30

que capturan "la fila esta produciendo mejor/peor que su historico".

API
---
La forma canonica de usar este modulo es a traves de
`LagFeatureTransformer` (sklearn-compat) como PRIMER paso del Pipeline
de preprocesamiento. Asi:

    Pipeline([
        ("lag_features", LagFeatureTransformer()),
        ("missing_flags", MissingFlagger()),
        ...
    ])

El transformer:
  - En `fit(X, y)` memoriza el historial necesario (FUNDO, FORMATO,
    FECHA, KG/HA, target). Ese historial viaja serializado dentro del
    pipeline cuando MLflow guarda el modelo.
  - En `fit_transform(X, y)` ademas devuelve X con los 31 lag features
    calculados sobre el train fold (sin leakage cross-fold).
  - En `transform(X_new)` calcula lags para filas nuevas usando solo el
    historial memorizado. Permite que el backend serve solo necesite
    enviar las 9 columnas raw.

La funcion publica `add_lag_features(df)` se mantiene para compatibilidad
y como helper interno del transformer.

Mejora vs implementacion anterior (en data_loader, pre-CV)
----------------------------------------------------------
Antes los lags se calculaban sobre TODO el dataset antes del CV split, lo
que mezclaba info de test folds con train folds (leakage moderado). Con
el transformer adentro del pipeline, cada fold solo ve su propio train
para construir el historial.

Cold start
----------
Filas sin >=3 observaciones previas en su grupo reciben sentinel
`COLD_START_FILL_VALUE` (-1) y la flag `LAG_FF_COLD=1`. Los modelos
de arbol manejan -1 como una hoja distinta sin necesidad de
preprocessamiento adicional. Los ratios cold-start tambien son -1.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin

from src.config import DATE_COLUMN, TARGET

logger = logging.getLogger(__name__)

WINDOWS: Tuple[int, ...] = (7, 14, 30, 90)
MIN_PERIODS = 3
COLD_START_FILL_VALUE = -1.0
KG_HA_COL = "KG/HA"

# Lag estacional: ventana centrada en (fecha - 365d) con tolerancia +/-15d.
# Captura ciclo agronómico anual (mismo periodo del año anterior por FUNDO+FORMATO).
SEASONAL_PERIOD_DAYS: int = 365
SEASONAL_TOLERANCE_DAYS: int = 15

# Grupos a calcular: nombre_corto -> columnas de groupby
GROUP_DEFS: list[Tuple[str, list[str]]] = [
    ("FF", ["FUNDO", "FORMATO"]),  # combinacion (mas especifica, menos densidad)
    ("F",  ["FUNDO"]),               # solo fundo (mas densidad)
    ("FMT", ["FORMATO"]),            # solo formato
]


def _rolling_lag(df_sorted: pd.DataFrame, value_col: str, group_cols: list[str], window: int) -> pd.Series:
    """Mediana rolling EXCLUYENDO la fila actual (shift(1) + rolling)."""
    return (
        df_sorted.groupby(group_cols, sort=False)[value_col]
        .transform(lambda s: s.shift(1).rolling(window, min_periods=MIN_PERIODS).median())
    )


def _safe_ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    """num/den evitando div por cero o por sentinel (-1)."""
    den_safe = den.where((den > 0), other=np.nan)
    return num / den_safe


def _seasonal_lag_for_group(dates_d: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Mediana estacional para UN grupo (FUNDO+FORMATO).

    Para cada fila i, mediana de `values` en filas cuya fecha cae en
    [date_i - (365+15)d, date_i - (365-15)d]. Asume `dates_d` ordenado
    ascendente. Devuelve NaN cuando hay <MIN_PERIODS observaciones en la
    ventana (típicamente filas del primer año del dataset).
    """
    n = len(dates_d)
    out = np.full(n, np.nan, dtype=float)
    delta_lo = np.timedelta64(SEASONAL_PERIOD_DAYS + SEASONAL_TOLERANCE_DAYS, "D")
    delta_hi = np.timedelta64(SEASONAL_PERIOD_DAYS - SEASONAL_TOLERANCE_DAYS, "D")
    target_lo = dates_d - delta_lo
    target_hi = dates_d - delta_hi
    for i in range(n):
        lo_idx = np.searchsorted(dates_d, target_lo[i], side="left")
        hi_idx = np.searchsorted(dates_d, target_hi[i], side="right")
        if hi_idx - lo_idx >= MIN_PERIODS:
            out[i] = np.median(values[lo_idx:hi_idx])
    return out


def add_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega lags + ratios. Devuelve df con orden original preservado.

    Por cada (FUNDO+FORMATO, FUNDO, FORMATO) x (KG_JR_H, KG_HA) x (30, 90):
        <value>_lag_<grupo>_<window>      mediana de las N obs anteriores

    Ratios "actual vs lag" (solo para KG_HA, que NO es leakage):
        KG_HA_ratio_FF_30 / _90           KG/HA actual / lag respectivo

    Flags:
        LAG_FF_COLD                       1 si grupo FUNDO+FORMATO sin historia
    """
    needed = [c for grp in GROUP_DEFS for c in grp[1]] + [DATE_COLUMN, TARGET, KG_HA_COL]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(f"add_lag_features: columnas faltantes: {missing}")

    # Para que groupby+rolling sea correcto, ordenamos POR cada agrupacion antes
    # de cada calculo. Conservamos el index original para restaurarlo al final.
    df_work = df.copy()

    new_cols: list[str] = []

    for alias, group_cols in GROUP_DEFS:
        df_sorted = df_work.sort_values(group_cols + [DATE_COLUMN])
        for value_col, vname in [(TARGET, "KG_JR_H"), (KG_HA_COL, "KG_HA")]:
            for w in WINDOWS:
                name = f"{vname}_lag_{alias}_{w}"
                df_work.loc[df_sorted.index, name] = _rolling_lag(
                    df_sorted, value_col, group_cols, w
                )
                new_cols.append(name)
    # NOTA: las flags LAG_FF_COLD y LAG_FF_SEASONAL_COLD existian aqui para
    # marcar filas sin historia. Se eliminaron tras permutation_importance
    # (mayo 2026) que mostro importance ~0 / negativa: el sentinel -1 ya
    # comunica el cold-start a los arboles, la flag binaria era redundante.

    # Lag estacional (mismo periodo del año anterior, ventana +/-15d). Solo
    # para grupo FF. Captura ciclo agronómico anual que rolling 90d no ve.
    df_sorted_ff = df_work.sort_values(["FUNDO", "FORMATO", DATE_COLUMN])
    dates_d_all = pd.to_datetime(df_sorted_ff[DATE_COLUMN]).values.astype("datetime64[D]")
    seasonal_cols: list[str] = []
    for value_col, vname in [(TARGET, "KG_JR_H"), (KG_HA_COL, "KG_HA")]:
        name = f"{vname}_lag_FF_seasonal"
        seasonal_arr = np.full(len(df_sorted_ff), np.nan, dtype=float)
        vals_all = df_sorted_ff[value_col].values.astype(float)
        for _, pos_arr in df_sorted_ff.groupby(["FUNDO", "FORMATO"], sort=False).indices.items():
            seasonal_arr[pos_arr] = _seasonal_lag_for_group(
                dates_d_all[pos_arr], vals_all[pos_arr]
            )
        df_work.loc[df_sorted_ff.index, name] = seasonal_arr
        seasonal_cols.append(name)
        new_cols.append(name)

    # Conteo de filas con cold-start (solo informativo para el log).
    n_cold_pre = int(df_work[[c for c in new_cols if "_lag_FF_" in c]].isna().all(axis=1).sum())
    n_cold_seasonal_pre = int(df_work[seasonal_cols].isna().all(axis=1).sum())

    # Ratios actual vs lag (solo KG_HA: NO usa el target)
    a30, a90 = "KG_HA_lag_FF_30", "KG_HA_lag_FF_90"
    df_work["KG_HA_ratio_FF_30"] = _safe_ratio(df_work[KG_HA_COL], df_work[a30])
    df_work["KG_HA_ratio_FF_90"] = _safe_ratio(df_work[KG_HA_COL], df_work[a90])
    new_cols += ["KG_HA_ratio_FF_30", "KG_HA_ratio_FF_90"]

    # delta_KG_JR_H_30_90 (ratio short/long del target)
    a, b = "KG_JR_H_lag_FF_30", "KG_JR_H_lag_FF_90"
    df_work["delta_KG_JR_H_30_90"] = _safe_ratio(df_work[a], df_work[b])
    new_cols.append("delta_KG_JR_H_30_90")

    # Imputar sentinel en TODO lo nuevo (incluyendo ratios)
    for c in new_cols:
        df_work[c] = df_work[c].fillna(COLD_START_FILL_VALUE)

    # Bajado a DEBUG porque ahora se llama POR cada pipeline.fit() dentro
    # de Optuna nested CV (~4500 veces para tuning prod), antes era 1 vez
    # en data_loader. Para verlo, configurar `logger.setLevel(DEBUG)` en
    # el caller, o subir a INFO temporalmente para diagnostico.
    logger.debug(
        f"Lag features agregadas | grupos={[g[0] for g in GROUP_DEFS]} | "
        f"cold_start_FF={n_cold_pre} ({n_cold_pre/len(df_work)*100:.1f}%) | "
        f"cold_seasonal={n_cold_seasonal_pre} ({n_cold_seasonal_pre/len(df_work)*100:.1f}%) | "
        f"n_nuevas_cols={len(new_cols)}"
    )

    return df_work


# ---------------------------------------------------------------------------
# Sklearn transformer wrapper
# ---------------------------------------------------------------------------
LAG_OUTPUT_COLUMNS: List[str] = [
    f"{vname}_lag_{alias}_{w}"
    for alias, _ in GROUP_DEFS
    for vname in ("KG_JR_H", "KG_HA")
    for w in WINDOWS
] + [
    "KG_JR_H_lag_FF_seasonal",
    "KG_HA_lag_FF_seasonal",
    "KG_HA_ratio_FF_30",
    "KG_HA_ratio_FF_90",
    "delta_KG_JR_H_30_90",
]

# Columnas raw que el transformer necesita para hacer su trabajo. El resto
# de columnas de X pasan tal cual al output.
_HISTORY_COLS: List[str] = ["FUNDO", "FORMATO", DATE_COLUMN, KG_HA_COL]


class LagFeatureTransformer(BaseEstimator, TransformerMixin):
    """Stateful transformer que calcula lags rolling/seasonal/ratios.

    Encapsula `add_lag_features` para que se ejecute DENTRO del Pipeline
    de sklearn. Memoriza el historial necesario en `fit`; en `transform`
    consulta ese historial para calcular lags de filas nuevas.

    Diseño
    ------
    - `fit(X, y)`        : guarda `self.history_` con (FUNDO, FORMATO,
      FECHA, KG/HA, target) extraido de los datos de entrenamiento.
    - `fit_transform(X, y)` : ademas devuelve X con las 31 columnas de
      lag agregadas, calculadas sobre el propio train (esto es lo que
      ven los siguientes pasos del pipeline durante fit).
    - `transform(X_new)` : combina history + filas nuevas (con TARGET=NaN
      para las nuevas), llama a `add_lag_features` y devuelve solo las
      filas nuevas con lags. Para inferencia el caller no necesita
      conocer el historial.

    Comportamiento ante NaN del target
    ----------------------------------
    `pandas.rolling(...).median()` ignora NaN, asi que las filas nuevas
    no contaminan los lags de filas historicas (aunque esos resultados
    los descartamos igualmente).
    """

    def __init__(self) -> None:
        # Sin hiperparametros tuneables: ventanas/sentinel son constantes
        # globales del modulo. Mantener __init__ vacio respeta el contrato
        # sklearn (no se debe hacer trabajo aqui).
        pass

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------
    def _validate_input(self, X: pd.DataFrame) -> None:
        missing = [c for c in _HISTORY_COLS if c not in X.columns]
        if missing:
            raise ValueError(
                f"LagFeatureTransformer: columnas requeridas faltantes en X: {missing}"
            )

    def _build_history(self, X: pd.DataFrame, y) -> pd.DataFrame:
        """Crea snapshot historico minimo (FUNDO, FORMATO, FECHA, KG/HA, TARGET)."""
        history = X[_HISTORY_COLS].copy()
        history[TARGET] = (
            y.values if isinstance(y, pd.Series) else np.asarray(y, dtype=float)
        )
        # Normalizamos el index para que pd.concat en transform no produzca
        # duplicados confusos.
        return history.reset_index(drop=True)

    # ------------------------------------------------------------------
    # Sklearn API
    # ------------------------------------------------------------------
    def fit(self, X: pd.DataFrame, y=None) -> "LagFeatureTransformer":
        if y is None:
            raise ValueError(
                "LagFeatureTransformer.fit requiere y (KG/JR_H) para construir history_."
            )
        self._validate_input(X)
        self.history_ = self._build_history(X, y)
        return self

    def fit_transform(self, X: pd.DataFrame, y=None, **fit_params) -> pd.DataFrame:
        # 1. Memoriza historial.
        self.fit(X, y)
        # 2. Calcula lags sobre el propio train llamando a la implementacion
        #    canonica con (X + target). Devuelve X con las nuevas columnas.
        df = X.copy()
        df[TARGET] = (
            y.values if isinstance(y, pd.Series) else np.asarray(y, dtype=float)
        )
        df_with_lags = add_lag_features(df).drop(columns=[TARGET])
        # Cache de transient: permite que `final_pipeline.predict(X_train)`
        # (caso 'Aplicacion Total' en single_run.py) reutilice los lags ya
        # computados en fit_transform en vez de pasar por el camino `transform`
        # que duplicaria filas y produciria lags con leakage o ventana
        # diluida. Se descarta al picklear (`__getstate__`) para no inflar
        # el artifact MLflow ni filtrarse a inferencia.
        self._fit_X_ref_ = X
        self._fit_output_ = df_with_lags
        return df_with_lags

    def __getstate__(self):
        state = self.__dict__.copy()
        # Caches transient: no deben viajar en el pickle del modelo. En
        # inferencia, el LagFeatureTransformer recibe data nueva y `transform`
        # entra por el camino normal (history_ + filas nuevas).
        state.pop("_fit_X_ref_", None)
        state.pop("_fit_output_", None)
        return state

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        if not hasattr(self, "history_"):
            raise RuntimeError(
                "LagFeatureTransformer no fue ajustado. Llama fit/fit_transform primero."
            )
        # Atajo in-sample: si la pipeline llama transform con el MISMO objeto
        # que se uso en fit_transform, devolvemos los lags ya calculados.
        # Object identity (`is`) es estricto a proposito: cualquier copia
        # cae al camino normal.
        cached_X = getattr(self, "_fit_X_ref_", None)
        if cached_X is not None and X is cached_X:
            return self._fit_output_

        self._validate_input(X)

        X_work = X.copy().reset_index(drop=True)
        # __row_id preserva el orden original para reordenar al final.
        X_work["__row_id"] = np.arange(len(X_work))
        X_work["__is_new"] = True
        # add_lag_features requiere TARGET; en inferencia no lo tenemos.
        # NaN propaga correctamente por rolling.median (skipna).
        if TARGET not in X_work.columns:
            X_work[TARGET] = np.nan

        history = self.history_.copy()
        history["__row_id"] = -1
        history["__is_new"] = False

        # Alinear columnas: history tiene las minimas (_HISTORY_COLS+TARGET);
        # X_work puede traer mas columnas raw (DPC, %INDUS, etc). Para el
        # calculo solo importan _HISTORY_COLS+TARGET, asi que rellenamos las
        # faltantes en history con NaN.
        for col in X_work.columns:
            if col not in history.columns:
                history[col] = np.nan
        # Y a la inversa: si history trajera columnas que X_work no tiene
        # (no deberia pero defensivo), las descartamos.
        history = history[X_work.columns]

        combined = pd.concat([history, X_work], axis=0, ignore_index=True)
        combined_with_lags = add_lag_features(combined)

        new_only = (
            combined_with_lags[combined_with_lags["__is_new"]]
            .sort_values("__row_id")
            .reset_index(drop=True)
        )
        # Limpiar helpers y el placeholder de TARGET.
        drop_cols = ["__row_id", "__is_new"]
        if TARGET in new_only.columns and TARGET not in X.columns:
            drop_cols.append(TARGET)
        return new_only.drop(columns=drop_cols)

    def get_feature_names_out(self, input_features=None) -> List[str]:
        base: List[str] = list(input_features) if input_features is not None else []
        # Filtra TARGET si vino en input_features (no es output).
        base = [c for c in base if c != TARGET]
        return base + LAG_OUTPUT_COLUMNS

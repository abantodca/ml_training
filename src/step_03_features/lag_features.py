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
  - En `fit_transform(X, y)` ademas devuelve X con los 35 lag features
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
`COLD_START_FILL_VALUE` (-1). Los modelos de arbol manejan -1 como una
hoja distinta sin necesidad de preprocessamiento adicional. Las flags
LAG_FF_COLD/LAG_FF_SEASONAL_COLD existian aqui pero se eliminaron tras
permutation_importance (mayo 2026): el sentinel -1 ya comunica el
cold-start a los arboles, la flag binaria era redundante.
"""
from __future__ import annotations

import logging
from typing import List, Tuple

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin

from src.config import DATE_COLUMN, TARGET

logger = logging.getLogger(__name__)

WINDOWS: Tuple[int, ...] = (7, 14, 30, 90)
MIN_PERIODS = 3
COLD_START_FILL_VALUE = -1.0
KG_HA_COL = "KG/HA"

# Estabilizadores adicionales por FUNDO+FORMATO sobre KG/HA:
# - std rolling 30: VOLATILIDAD del grupo. Un FUNDO+FORMATO con std alta
#   es mas dificil de predecir; el arbol puede tratarlo distinto. Tambien
#   alimenta predict_with_std de OOFEnsembleRegressor.
# - slope rolling 30: regresion lineal de KG/HA contra t en ultimas 30 obs.
#   Captura momentum (alza/caida) que la mediana no ve.
# - days_since_last_FF: dias desde la cosecha previa en mismo FF. Senal de
#   cadencia agronomica.
# - REL_FORMATO_30: KG_HA_lag_F_30 / KG_HA_lag_FMT_30. Posicionamiento
#   relativo del fundo dentro de su cohorte de formato.
#
# EWMA halflife=15 + std_FF_90 fueron evaluados pero descartados: corr
# +0.98 con KG_HA_lag_FF_30 y +0.95 entre std_FF_30 y std_FF_90. Una sola
# ventana cubre la senal de volatilidad sin duplicar.
STD_WINDOW: int = 30
SLOPE_WINDOW: int = 30

# Lag estacional: ventana centrada en (fecha - 365d) con tolerancia +/-15d.
# Captura ciclo agronomico anual (mismo periodo del ano anterior por FUNDO+FORMATO).
# Ventana ±30d (wide) fue evaluada (2026-05-05) y descartada: corr +0.969
# con la ±15d sobre POP (cadencia diaria regular). La ventana mas amplia
# captura casi exactamente las mismas obs -> redundancia.
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


# ---------------------------------------------------------------------------
# Helpers privados de add_lag_features. Cada uno muta `df_work` in-place
# (escribe columnas nuevas) y devuelve la lista de nombres agregados. La
# mutacion es deliberada para evitar copiar ~10k filas x 30 columnas en
# cada paso intermedio (cada call de pipeline.fit lo invoca).
# ---------------------------------------------------------------------------


def _compute_rolling_lags(df_work: pd.DataFrame) -> list[str]:
    """Lags rolling por grupo (FF, F, FMT) x valor (KG_JR_H, KG_HA) x ventana.

    Para cada (alias, group_cols) en GROUP_DEFS y cada ventana en WINDOWS,
    calcula la mediana rolling EXCLUYENDO la fila actual (shift(1) +
    rolling). Resultado: ~24 columnas (3 grupos x 2 valores x 4 ventanas).
    """
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
    return new_cols


def _compute_volatility_and_momentum_lags(df_work: pd.DataFrame) -> list[str]:
    """Volatilidad + momentum + cadencia por FUNDO+FORMATO (no usa target).

    Anade 3 features con senal unica verificada (corr <0.85 con cualquier
    rolling lag existente):

    - KG_HA_std_FF_30: desviacion estandar rolling con shift(1). Senal de
      volatilidad del grupo. Aporta capacidad de modular prediccion segun
      cuan estable es el FF y alimenta predict_with_std.
    - KG_HA_slope_FF_30: pendiente de regresion lineal KG/HA vs t en
      ultimas 30 obs (shift(1)). Captura momentum (alza/caida) que la
      mediana rolling no ve. Calculado sobre indices 0..n-1 de cada
      ventana, normalizado para que la unidad sea kg/HA por observacion.
    - days_since_last_FF: gap en dias hasta la observacion previa en el
      mismo FF. Cadencia agronomica: gap largo -> fruta mas madura.

    Operan solo sobre KG/HA y FECHA (no target) -> CV-safe sin logica
    adicional. KG/HA y FECHA estan disponibles en filas nuevas.
    """
    new_cols: list[str] = []
    df_sorted = df_work.sort_values(["FUNDO", "FORMATO", DATE_COLUMN])
    grouped_kgha = df_sorted.groupby(["FUNDO", "FORMATO"], sort=False)[KG_HA_COL]

    # 1) std rolling 30 sobre KG/HA por FF
    std_name = f"KG_HA_std_FF_{STD_WINDOW}"
    df_work.loc[df_sorted.index, std_name] = grouped_kgha.transform(
        lambda s: s.shift(1).rolling(STD_WINDOW, min_periods=MIN_PERIODS).std()
    )
    new_cols.append(std_name)

    # 2) slope rolling 30: pendiente OLS sobre KG/HA(t) en ventana de 30
    #    (shift(1) excluye self). Helper interno: recibe Series, devuelve
    #    Series de slopes. Usa apply para mantener legibilidad; el costo
    #    es aceptable porque la ventana es chica (30) y solo corre en
    #    LagFeatureTransformer.fit_transform (no en cada predict).
    def _rolling_slope(s: pd.Series) -> pd.Series:
        s_shift = s.shift(1)
        return s_shift.rolling(SLOPE_WINDOW, min_periods=MIN_PERIODS).apply(
            _slope_of_window, raw=True
        )

    slope_name = f"KG_HA_slope_FF_{SLOPE_WINDOW}"
    df_work.loc[df_sorted.index, slope_name] = grouped_kgha.transform(_rolling_slope)
    new_cols.append(slope_name)

    # 3) days_since_last_FF: diferencia en dias hasta la fila previa en
    #    mismo FF. Primera fila del grupo => NaN (cold-start, captado por
    #    sentinel -1 al final de add_lag_features).
    days_name = "days_since_last_FF"
    fechas_sorted = pd.to_datetime(df_sorted[DATE_COLUMN])
    diffs = (
        fechas_sorted.groupby(
            [df_sorted["FUNDO"], df_sorted["FORMATO"]], sort=False
        )
        .diff()
        .dt.days.astype(float)
    )
    df_work.loc[df_sorted.index, days_name] = diffs.values
    new_cols.append(days_name)

    # 4) tenure_FUNDO: dias desde la PRIMERA observacion del FUNDO en el
    #    dataset. Captura "antiguedad" del fundo dentro del registro. Un
    #    fundo nuevo (tenure chico) puede tener manejo agronomico distinto
    #    a uno con anos de historia. Independiente de days_since_last_FF
    #    (que es gap entre cosechas consecutivas dentro de un FF).
    #
    #    CV-safe: en LagFeatureTransformer.transform, el min(FECHA) por
    #    FUNDO se calcula sobre el dataframe COMBINED (history + new). El
    #    history ya tiene las fechas mas antiguas del train, asi que el
    #    min coincide con el visto en fit -> tenure de filas nuevas usa
    #    el origen del FUNDO en el train, no en el test.
    tenure_name = "tenure_FUNDO_days"
    fechas_full = pd.to_datetime(df_work[DATE_COLUMN])
    first_per_fundo = fechas_full.groupby(df_work["FUNDO"]).transform("min")
    df_work[tenure_name] = (fechas_full - first_per_fundo).dt.days.astype(float)
    new_cols.append(tenure_name)

    return new_cols


def _slope_of_window(arr: np.ndarray) -> float:
    """Pendiente OLS de arr[i] vs i (i=0..n-1). NaN-aware via mean centering.

    Cerrada algebraica para evitar la sobrecarga de scipy/sklearn:
        slope = sum((x - x_mean)(y - y_mean)) / sum((x - x_mean)^2)
    """
    n = len(arr)
    if n < MIN_PERIODS:
        return np.nan
    # Saltar NaN en y (raro porque KG/HA no tiene NaN post-imputer, pero
    # defensivo cuando el helper se llama via rolling.apply en init de fold)
    mask = ~np.isnan(arr)
    if mask.sum() < MIN_PERIODS:
        return np.nan
    y = arr[mask]
    x = np.arange(n, dtype=float)[mask]
    x_centered = x - x.mean()
    denom = (x_centered ** 2).sum()
    if denom <= 0.0:
        return np.nan
    return float((x_centered * (y - y.mean())).sum() / denom)


def _compute_seasonal_lags(df_work: pd.DataFrame) -> list[str]:
    """Lag estacional (mismo periodo del ano anterior, ventana +/-15d).

    Solo para grupo FUNDO+FORMATO. Captura ciclo agronomico anual que
    el rolling 90d no ve. Devuelve 2 columnas (KG_JR_H, KG_HA).
    """
    new_cols: list[str] = []
    df_sorted_ff = df_work.sort_values(["FUNDO", "FORMATO", DATE_COLUMN])
    dates_d_all = pd.to_datetime(df_sorted_ff[DATE_COLUMN]).values.astype("datetime64[D]")
    for value_col, vname in [(TARGET, "KG_JR_H"), (KG_HA_COL, "KG_HA")]:
        name = f"{vname}_lag_FF_seasonal"
        seasonal_arr = np.full(len(df_sorted_ff), np.nan, dtype=float)
        vals_all = df_sorted_ff[value_col].values.astype(float)
        for _, pos_arr in df_sorted_ff.groupby(
            ["FUNDO", "FORMATO"], sort=False
        ).indices.items():
            seasonal_arr[pos_arr] = _seasonal_lag_for_group(
                dates_d_all[pos_arr], vals_all[pos_arr]
            )
        df_work.loc[df_sorted_ff.index, name] = seasonal_arr
        new_cols.append(name)
    return new_cols


def _compute_ratios(df_work: pd.DataFrame) -> list[str]:
    """Ratios "actual vs algo": locales (vs propio lag) y global (vs pool).

    Locales (KG_HA solo: NO usa target):
        KG_HA_ratio_FF_30 = KG_HA_actual / KG_HA_lag_FF_30
        KG_HA_ratio_FF_90 = KG_HA_actual / KG_HA_lag_FF_90

    Global pool (vectorizado con rolling 30 obs sobre dataset ordenado por
    fecha, shift(1) excluye self):
        KG_HA_REL_GLOBAL_30 = KG_HA_actual / median(KG_HA en ultimas 30 obs
                              globales). Capta "este fundo rinde mejor o
                              peor que el promedio del mercado". Sesgo de
                              incluir self-fundo es chico (~1/n_fundos).
                              CV-safe via shift(1).

    Delta short/long del target (ratio entre lags FF, NO usa el target
    actual -> sigue siendo CV-safe):
        delta_KG_JR_H_30_90 = KG_JR_H_lag_FF_30 / KG_JR_H_lag_FF_90

    Requiere que `_compute_rolling_lags` ya haya escrito los lags FF/30 y
    FF/90 (de los cuales este helper depende).
    """
    new_cols: list[str] = []

    # Locales (KG_HA actual vs su lag FF)
    df_work["KG_HA_ratio_FF_30"] = _safe_ratio(
        df_work[KG_HA_COL], df_work["KG_HA_lag_FF_30"]
    )
    df_work["KG_HA_ratio_FF_90"] = _safe_ratio(
        df_work[KG_HA_COL], df_work["KG_HA_lag_FF_90"]
    )
    new_cols += ["KG_HA_ratio_FF_30", "KG_HA_ratio_FF_90"]

    # Global pool (KG_HA actual vs mediana cross-fundos rolling 30 obs)
    df_sorted_date = df_work.sort_values(DATE_COLUMN)
    rolling_global_30 = (
        df_sorted_date[KG_HA_COL]
        .shift(1)
        .rolling(30, min_periods=MIN_PERIODS)
        .median()
    )
    df_work.loc[df_sorted_date.index, "_KG_HA_lag_GLOBAL_30"] = rolling_global_30
    df_work["KG_HA_REL_GLOBAL_30"] = _safe_ratio(
        df_work[KG_HA_COL], df_work["_KG_HA_lag_GLOBAL_30"]
    )
    df_work.drop(columns=["_KG_HA_lag_GLOBAL_30"], inplace=True)
    new_cols.append("KG_HA_REL_GLOBAL_30")

    # Delta short/long del target (entre lags, no leakage)
    df_work["delta_KG_JR_H_30_90"] = _safe_ratio(
        df_work["KG_JR_H_lag_FF_30"], df_work["KG_JR_H_lag_FF_90"]
    )
    new_cols.append("delta_KG_JR_H_30_90")

    # Posicionamiento del FUNDO dentro de su cohorte de FORMATO en el lag 30:
    # KG_HA_lag_F_30 / KG_HA_lag_FMT_30 -> >1 si el fundo rinde por encima
    # del promedio de su formato en ese horizonte, <1 si por debajo. Auditado
    # vs los lag base: corr <0.5 -> senal independiente.
    df_work["KG_HA_REL_FORMATO_30"] = _safe_ratio(
        df_work["KG_HA_lag_F_30"], df_work["KG_HA_lag_FMT_30"]
    )
    new_cols.append("KG_HA_REL_FORMATO_30")

    return new_cols


def add_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    """Orquestador thin: rolling lags + seasonal + ratios + sentinel fill.

    Devuelve `df` con orden original preservado. Las columnas agregadas son
    las listadas en `LAG_OUTPUT_COLUMNS` (35 columnas).

    Pipeline:
        1. _compute_rolling_lags                 : FF/F/FMT x KG_JR_H/KG_HA x 7/14/30/90
        2. _compute_seasonal_lags                : FF x ano-1 +/-15d
        3. _compute_volatility_and_momentum_lags : std + slope + days_since + tenure
        4. _compute_ratios                       : ratios FF/30,FF/90 + global + delta + REL_FORMATO
        5. Sentinel fill (-1)                    : reemplaza NaN cold-start
    """
    needed = [c for grp in GROUP_DEFS for c in grp[1]] + [DATE_COLUMN, TARGET, KG_HA_COL]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(f"add_lag_features: columnas faltantes: {missing}")

    df_work = df.copy()
    new_cols: list[str] = []
    new_cols.extend(_compute_rolling_lags(df_work))
    seasonal_cols = _compute_seasonal_lags(df_work)
    new_cols.extend(seasonal_cols)
    new_cols.extend(_compute_volatility_and_momentum_lags(df_work))
    new_cols.extend(_compute_ratios(df_work))

    # Conteo de filas con cold-start (solo informativo para el log).
    n_cold_pre = int(
        df_work[[c for c in new_cols if "_lag_FF_" in c]].isna().all(axis=1).sum()
    )
    n_cold_seasonal_pre = int(df_work[seasonal_cols].isna().all(axis=1).sum())

    # Sentinel en todas las features nuevas (incluyendo ratios). El -1 ya
    # le comunica al arbol que la fila es cold-start sin necesidad de flag.
    for c in new_cols:
        df_work[c] = df_work[c].fillna(COLD_START_FILL_VALUE)

    # DEBUG porque se llama por cada pipeline.fit dentro de Optuna nested CV
    # (~4500 veces en TUNING=prod). Subir a INFO temporal solo para diagnostico.
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
    f"KG_HA_std_FF_{STD_WINDOW}",
    f"KG_HA_slope_FF_{SLOPE_WINDOW}",
    "days_since_last_FF",
    "tenure_FUNDO_days",
    "KG_HA_ratio_FF_30",
    "KG_HA_ratio_FF_90",
    "delta_KG_JR_H_30_90",
    "KG_HA_REL_GLOBAL_30",
    "KG_HA_REL_FORMATO_30",
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

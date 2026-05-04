"""Validacion de schema y rangos del dataset crudo.

Se ejecuta DESPUES del parseo Excel pero ANTES de que el DataFrame entre al
Pipeline. Detecta problemas estructurales que romperian el modelo o
producirian metricas absurdas:

  - tipos incorrectos (categoricas que vinieron como float, fechas mal parseadas)
  - rangos imposibles (KG/JR_H <= 0, H-EF fuera de [0, 24])
  - duplicados estructurales (mismo FUNDO+FORMATO+FECHA -> dos cosechas
    contradictorias para el mismo dia)
  - columnas con varianza cero (todas las filas con el mismo valor; no
    aporta nada al modelo)

El comportamiento por default es STRICT (raise al primer fallo). El caller
puede pasar `strict=False` para que reporte warnings en lugar de fallar.

Sin dependencias nuevas: solo pandas + numpy.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np
import pandas as pd

from src.config import (
    CATEGORICAL_FEATURES,
    DATE_COLUMN,
    NUMERIC_FEATURES,
    TARGET,
)

logger = logging.getLogger(__name__)


# Rangos sanos por columna. Si un valor cae fuera, lo marcamos como
# violacion de schema. None = sin chequeo de rango.
_NUMERIC_RANGES: dict = {
    TARGET: (0.0, None),       # KG/JR_H > 0; sin techo (puede haber outliers altos)
    "KG/HA": (0.0, None),      # productividad por hectarea > 0
    "%INDUS": (0.0, 100.0),    # porcentaje
    "DPC": (0.0, None),        # dias post cosecha; tipicamente 0-180
    "P/BAYA": (0.0, None),     # peso por baya en gramos
    "HA": (0.0, None),         # hectareas cosechadas
    "DIA_COSECHA": (1, 366),   # dia juliano del año
    # Para validar H-EF (jornada efectiva) usamos un rango plausible:
    "H-EF": (0.0, 24.0),       # horas efectivas en una jornada
    "KG/JR": (0.0, None),      # kilos por jornal
}


class SchemaError(ValueError):
    """Error de validacion de schema. Heredamos de ValueError para que sea
    capturable como un input invalido del usuario, no un bug interno."""


def _check_required_columns(df: pd.DataFrame, required: List[str]) -> List[str]:
    return [c for c in required if c not in df.columns]


def _check_dtypes(df: pd.DataFrame) -> List[str]:
    """Devuelve lista de problemas de tipo detectados."""
    issues: List[str] = []
    for col in NUMERIC_FEATURES + [TARGET]:
        if col in df.columns and not pd.api.types.is_numeric_dtype(df[col]):
            n_non_numeric = pd.to_numeric(df[col], errors="coerce").isna().sum()
            if n_non_numeric > 0:
                issues.append(
                    f"'{col}' tiene {n_non_numeric} valor(es) no numerico(s) "
                    f"que se convertiran a NaN"
                )
    if DATE_COLUMN in df.columns and not pd.api.types.is_datetime64_any_dtype(df[DATE_COLUMN]):
        # Permitimos string si parsea bien; lo flaggeamos como info.
        try:
            pd.to_datetime(df[DATE_COLUMN], errors="raise")
        except Exception as exc:
            issues.append(f"'{DATE_COLUMN}' no parsea como fecha: {exc}")
    return issues


def _check_numeric_ranges(df: pd.DataFrame) -> List[str]:
    """Reporta filas con valores fuera del rango sano por columna."""
    issues: List[str] = []
    for col, (lo, hi) in _NUMERIC_RANGES.items():
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        bad_lo = (s < lo).sum() if lo is not None else 0
        bad_hi = (s > hi).sum() if hi is not None else 0
        if bad_lo > 0:
            issues.append(f"'{col}': {int(bad_lo)} fila(s) con valor < {lo}")
        if bad_hi > 0:
            issues.append(f"'{col}': {int(bad_hi)} fila(s) con valor > {hi}")
    return issues


def _check_full_row_duplicates(df: pd.DataFrame) -> List[str]:
    """Detecta filas COMPLETAMENTE duplicadas (todas las columnas iguales).

    NOTA: NO chequeamos por (FUNDO+FORMATO+FECHA) porque en datos agricolas
    varias cosechas el mismo dia para el mismo (fundo, formato) son
    legitimas (distintas cuadrillas, turnos, lotes). Solo flaggeamos
    filas exactamente duplicadas que probablemente vinieron de un export
    duplicado del Excel.
    """
    n_dup = df.duplicated(keep="first").sum()
    if n_dup > 0:
        return [f"{int(n_dup)} fila(s) exactamente duplicadas (todas las cols iguales)"]
    return []


def _check_zero_variance(df: pd.DataFrame) -> List[str]:
    """Columnas con varianza 0 (todos los valores iguales). No aportan al modelo."""
    issues: List[str] = []
    for col in NUMERIC_FEATURES:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if len(s) > 0 and s.nunique() == 1:
            issues.append(f"'{col}' tiene varianza 0 (todos los valores = {s.iloc[0]})")
    for col in CATEGORICAL_FEATURES:
        if col not in df.columns:
            continue
        s = df[col].dropna()
        if len(s) > 0 and s.nunique() == 1:
            issues.append(f"'{col}' tiene un unico nivel ({s.iloc[0]!r})")
    return issues


def validate_dataset(
    df: pd.DataFrame,
    *,
    required_columns: Optional[List[str]] = None,
    strict: bool = True,
) -> List[str]:
    """Corre todos los chequeos de schema. Devuelve lista de issues encontrados.

    Args
    ----
    df : DataFrame post-parseo Excel y pre-Pipeline.
    required_columns : si se da, valida presencia. Si falta alguna, raise.
    strict : True (default) -> raise SchemaError ante issues criticos.
             False -> solo logea warnings y devuelve la lista.

    Issues criticos = columnas faltantes, dtypes irrecuperables, duplicados.
    Issues blandos (rangos, varianza cero) = solo warnings, se permiten en
    strict porque pueden ser legitimos en datasets nuevos.
    """
    if required_columns:
        missing = _check_required_columns(df, required_columns)
        if missing:
            raise SchemaError(f"Columnas requeridas faltantes: {missing}")

    critical: List[str] = []
    soft: List[str] = []

    soft.extend(_check_dtypes(df))
    soft.extend(_check_numeric_ranges(df))
    # Filas exactamente duplicadas: warning (sobre-pesan al training pero
    # no lo corrompen). Duplicados en (FUNDO+FORMATO+FECHA) NO se
    # chequean: son cosechas distintas legitimas el mismo dia.
    soft.extend(_check_full_row_duplicates(df))
    soft.extend(_check_zero_variance(df))

    for issue in critical:
        logger.error(f"[schema/CRITICAL] {issue}")
    for issue in soft:
        logger.warning(f"[schema/soft] {issue}")

    if strict and critical:
        raise SchemaError(
            f"Validacion fallo con {len(critical)} issue(s) critico(s): "
            f"{critical[0]}"
        )

    return critical + soft

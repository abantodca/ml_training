"""Transformaciones del target para estabilizar varianza y limitar outliers.

Construye un `TransformedTargetRegressor` que envuelve el modelo y aplica
en el espacio de y:
    func        : y -> log1p(min(y, p99.5(y)))   (cap suave + log)
    inverse_func: log -> expm1                   (sin descap, predict honesto)

El cap se calcula DENTRO del fold (TTR.fit recibe el y_train del fold),
asi que es CV-safe: el y_test del scoring no pasa por func.

Nota historica iter1->iter2: empezamos con cap p99 (1% extremos
aplastados) que mejoro deciles 1-3 pero acentuo subestimacion del decil
10 (-7.34 -> -8.64). Aflojamos a p99.5 (0.5% mas alto) para recuperar
predicciones extremas sin perder estabilidad.
"""
from __future__ import annotations

import numpy as np
from sklearn.base import BaseEstimator
from sklearn.compose import TransformedTargetRegressor

CAP_PERCENTILE: float = 99.5  # p99.5 -> ~0.5% de filas aplastadas en train


def _log1p_cap(y):
    """Cap suave (CAP_PERCENTILE) y luego log1p para estabilizar varianza."""
    y = np.asarray(y, dtype=float)
    cap = float(np.nanpercentile(y, CAP_PERCENTILE))
    return np.log1p(np.minimum(y, cap))


def _expm1(y):
    return np.expm1(np.asarray(y, dtype=float))


def wrap_with_log_target(regressor: BaseEstimator) -> TransformedTargetRegressor:
    """Envuelve un regresor con log1p+cap suave.

    `check_inverse=False` porque el cap no es invertible (perdida de info
    intencionada en el lado de y_train para ganar generalizacion).
    """
    return TransformedTargetRegressor(
        regressor=regressor,
        func=_log1p_cap,
        inverse_func=_expm1,
        check_inverse=False,
    )


def unwrap_regressor(estimator: BaseEstimator) -> BaseEstimator:
    """Devuelve el regresor 'real' (LGBM/XGB) si esta envuelto en TTR."""
    if isinstance(estimator, TransformedTargetRegressor):
        # Tras fit, el modelo entrenado vive en `regressor_` (con guion bajo).
        return getattr(estimator, "regressor_", estimator.regressor)
    return estimator

"""Factory de LGBMRegressor con defaults sanos.

Devuelve un `TransformedTargetRegressor` que aplica log1p+cap-p99.5 a y
durante el fit (CV-safe: cap calculado en cada fold). El predict ya
invierte al espacio original (KG/JR_H), asi que las metricas quedan
en unidades del target.
"""
from __future__ import annotations

from lightgbm import LGBMRegressor
from sklearn.compose import TransformedTargetRegressor

from src.step_04_train.target_transform import (
    _PARALLELISM_DOCSTRING,
    _common_kwargs,
    wrap_with_log_target,
)

_BACKEND_SPECIFIC = dict(
    verbose=-1,
    objective="regression_l1",
    subsample_freq=1,
)


def get_lgb_model(**overrides) -> TransformedTargetRegressor:
    """LGBMRegressor envuelto en TransformedTargetRegressor (log1p + cap p99.5).

    `objective='regression_l1'` (= MAE nativo). Alinea la loss interna con la
    metrica de seleccion (MAE en Optuna y MAPE de negocio para el campeon).

    {parallelism}

    `subsample_freq=1` fijo: aplicar bagging cada arbol (no cada N). Antes
    estaba en search space 1-7; valores altos anulaban el bagging porque solo
    1/N arboles veia un sample (resto entrenaba con dataset completo).
    """
    params = _common_kwargs() | _BACKEND_SPECIFIC
    params.update(overrides)
    return wrap_with_log_target(LGBMRegressor(**params))


get_lgb_model.__doc__ = get_lgb_model.__doc__.format(parallelism=_PARALLELISM_DOCSTRING)

"""Factory de LGBMRegressor con defaults sanos.

Devuelve un `TransformedTargetRegressor` que aplica log1p+cap-p99 a y
durante el fit (CV-safe: cap calculado en cada fold). El predict ya
invierte al espacio original (KG/JR_H), asi que las metricas quedan
en unidades del target.
"""
from __future__ import annotations

import os

from lightgbm import LGBMRegressor
from sklearn.compose import TransformedTargetRegressor

from src.config import RANDOM_STATE
from src.step_04_train.target_transform import wrap_with_log_target


def get_lgb_model(**overrides) -> TransformedTargetRegressor:
    """LGBMRegressor envuelto en TransformedTargetRegressor (log1p + cap p99).

    `objective='regression_l1'` (= MAE nativo). Alinea la loss interna con la
    metrica de seleccion (MAE en Optuna y MAPE de negocio para el campeon).

    `n_jobs` lee env var MODEL_N_JOBS (default -1 = todos los cores). Antes
    estaba hardcoded a 1, lo que desperdiciaba cores cuando se entrena UNA
    sola variedad (caso comun: `task train VARIETIES=POP`). Para multi-variedad
    en paralelo (--parallel-varieties >1) setear MODEL_N_JOBS=1 para evitar
    oversubscription (parallel-varieties * n_jobs > cores).

    `subsample_freq=1` fijo: aplicar bagging cada arbol (no cada N). Antes
    estaba en search space 1-7; valores altos anulaban el bagging porque solo
    1/N arboles veia un sample (resto entrenaba con dataset completo).
    """
    params = dict(
        random_state=RANDOM_STATE,
        n_jobs=int(os.environ.get("MODEL_N_JOBS", "-1")),
        verbose=-1,
        objective="regression_l1",
        subsample_freq=1,
    )
    params.update(overrides)
    return wrap_with_log_target(LGBMRegressor(**params))

"""Factory de XGBRegressor con defaults sanos.

Devuelve un `TransformedTargetRegressor` que aplica log1p+cap-p99 a y
durante el fit (CV-safe: cap calculado en cada fold). El predict ya
invierte al espacio original (KG/JR_H).
"""
from __future__ import annotations

import os

from sklearn.compose import TransformedTargetRegressor
from xgboost import XGBRegressor

from src.config import RANDOM_STATE
from src.step_04_train.target_transform import wrap_with_log_target


def get_xgb_model(**overrides) -> TransformedTargetRegressor:
    """XGBRegressor envuelto en TransformedTargetRegressor (log1p + cap p99).

    `objective='reg:absoluteerror'` (XGB >=1.7) -> MAE nativo, alineado con la
    metrica de seleccion (MAE de Optuna y MAPE de negocio del campeon). Antes
    se usaba el default `reg:squarederror` (L2), lo que entrenaba penalizando
    cuadraticamente y luego se evaluaba en MAE -> objetivo desalineado.

    `n_jobs` lee env var MODEL_N_JOBS (default -1 = todos los cores). Antes
    estaba hardcoded a 1, lo que desperdiciaba cores cuando se entrena UNA
    sola variedad (caso comun: `task train VARIETIES=POP`). Para multi-variedad
    en paralelo (--parallel-varieties >1) setear MODEL_N_JOBS=1 para evitar
    oversubscription (parallel-varieties * n_jobs > cores).
    """
    params = dict(
        random_state=RANDOM_STATE,
        n_jobs=int(os.environ.get("MODEL_N_JOBS", "-1")),
        verbosity=0,
        tree_method="hist",
        objective="reg:absoluteerror",
    )
    params.update(overrides)
    return wrap_with_log_target(XGBRegressor(**params))

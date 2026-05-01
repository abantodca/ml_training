"""Factory de XGBRegressor con defaults sanos.

Devuelve un `TransformedTargetRegressor` que aplica log1p+cap-p99 a y
durante el fit (CV-safe: cap calculado en cada fold). El predict ya
invierte al espacio original (KG/JR_H).
"""
from __future__ import annotations

from sklearn.compose import TransformedTargetRegressor
from xgboost import XGBRegressor

from src.config import RANDOM_STATE
from src.step_04_train.target_transform import wrap_with_log_target


def get_xgb_model(**overrides) -> TransformedTargetRegressor:
    """XGBRegressor envuelto en TransformedTargetRegressor (log1p + cap p99)."""
    params = dict(
        random_state=RANDOM_STATE,
        n_jobs=1,
        verbosity=0,
        tree_method="hist",
    )
    params.update(overrides)
    return wrap_with_log_target(XGBRegressor(**params))

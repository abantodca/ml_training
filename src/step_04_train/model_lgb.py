"""Factory de LGBMRegressor con defaults sanos.

Devuelve un `TransformedTargetRegressor` que aplica log1p+cap-p99 a y
durante el fit (CV-safe: cap calculado en cada fold). El predict ya
invierte al espacio original (KG/JR_H), asi que las metricas quedan
en unidades del target.
"""
from __future__ import annotations

from lightgbm import LGBMRegressor
from sklearn.compose import TransformedTargetRegressor

from src.config import RANDOM_STATE
from src.step_04_train.target_transform import wrap_with_log_target


def get_lgb_model(**overrides) -> TransformedTargetRegressor:
    """LGBMRegressor envuelto en TransformedTargetRegressor (log1p + cap p99).

    `objective='quantile' alpha=0.5` (= mediana). La pinball loss penaliza
    menos los outliers extremos que MSE. La metrica externa de Optuna sigue
    siendo MAE (cross_val_score) -- este objective solo afecta el split
    selection interno de LGB.
    """
    params = dict(
        random_state=RANDOM_STATE,
        n_jobs=1,
        verbose=-1,
        objective="quantile",
        alpha=0.5,
    )
    params.update(overrides)
    return wrap_with_log_target(LGBMRegressor(**params))

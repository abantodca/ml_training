"""Espacios de busqueda Optuna por backend.

Aislar los espacios aqui (en vez de mezclarlos con el bucle Nested CV)
permite:
    - Anadir un nuevo backend (catboost, ngboost, ...) editando UN solo
      archivo, sin tocar `tuning.py`.
    - Versionar cambios de search space sin churn en la logica de CV.
    - Reusar los espacios desde notebooks de exploracion.

Cada `_suggest_*` recibe el `optuna.Trial` y devuelve un dict de
`{nombre_param_pipeline: valor}` listo para `Pipeline.set_params(**dict)`.
Las claves usan el prefijo `regressor__` o `preprocessor__<step>__`
porque el pipeline final tiene la forma:

    Pipeline(steps=[("preprocessor", <pp>), ("regressor", <model>)])
"""
from __future__ import annotations

from typing import Dict

import optuna


# ---------------------------------------------------------------------------
# Preprocesador (compartido para todos los backends)
# ---------------------------------------------------------------------------


def suggest_preprocessor_params(trial: optuna.Trial) -> Dict[str, object]:
    """Hiperparametros del preprocesador que tambien tuneamos."""
    return {
        "preprocessor__imputer__n_neighbors": trial.suggest_int(
            "preprocessor__imputer__n_neighbors", 3, 25
        ),
        "preprocessor__outliers__method": trial.suggest_categorical(
            "preprocessor__outliers__method", ["iqr", "percentile"]
        ),
        "preprocessor__outliers__factor": trial.suggest_float(
            "preprocessor__outliers__factor", 1.5, 5.0
        ),
    }


# ---------------------------------------------------------------------------
# XGBoost
# ---------------------------------------------------------------------------


def suggest_xgb_params(trial: optuna.Trial) -> Dict[str, object]:
    """Search space anti-overfitting (rev. 4):
    - learning_rate piso 5e-3 -> 1e-2: evita undertraining con n_estimators<=1500.
    - Anade colsample_bylevel y colsample_bynode (regularizacion barata
      multiplicativa, recomendada por la doc de XGBoost).
    - Anade max_delta_step (estabiliza updates con target log1p + cap-p99).
    - Afloja min_child_weight 10-50 -> 3-30: para regression squared-error
      hessian ~= 1, asi que el rango anterior ahogaba hojas en FUNDOs chicos.
    """
    return {
        "regressor__regressor__n_estimators": trial.suggest_int(
            "regressor__regressor__n_estimators", 200, 1500
        ),
        "regressor__regressor__max_depth": trial.suggest_int(
            "regressor__regressor__max_depth", 3, 7
        ),
        "regressor__regressor__learning_rate": trial.suggest_float(
            "regressor__regressor__learning_rate", 1e-2, 0.3, log=True
        ),
        "regressor__regressor__subsample": trial.suggest_float(
            "regressor__regressor__subsample", 0.5, 1.0
        ),
        "regressor__regressor__colsample_bytree": trial.suggest_float(
            "regressor__regressor__colsample_bytree", 0.5, 1.0
        ),
        "regressor__regressor__colsample_bylevel": trial.suggest_float(
            "regressor__regressor__colsample_bylevel", 0.5, 1.0
        ),
        "regressor__regressor__colsample_bynode": trial.suggest_float(
            "regressor__regressor__colsample_bynode", 0.5, 1.0
        ),
        "regressor__regressor__min_child_weight": trial.suggest_float(
            "regressor__regressor__min_child_weight", 3.0, 30.0, log=True
        ),
        "regressor__regressor__gamma": trial.suggest_float(
            "regressor__regressor__gamma", 0.1, 10.0, log=True
        ),
        "regressor__regressor__max_delta_step": trial.suggest_int(
            "regressor__regressor__max_delta_step", 0, 7
        ),
        "regressor__regressor__reg_alpha": trial.suggest_float(
            "regressor__regressor__reg_alpha", 1e-3, 10.0, log=True
        ),
        "regressor__regressor__reg_lambda": trial.suggest_float(
            "regressor__regressor__reg_lambda", 1.0, 10.0, log=True
        ),
    }


# ---------------------------------------------------------------------------
# LightGBM
# ---------------------------------------------------------------------------


def suggest_lgb_params(trial: optuna.Trial) -> Dict[str, object]:
    """Search space anti-overfitting (rev. 4):
    - num_leaves condicionado a max_depth (cumple num_leaves <= 2^max_depth
      de la doc; antes parte del rango era inalcanzable y trial-time wasted).
    - bagging_freq desde 1 (antes 0): cuando salia 0, subsample quedaba
      inerte y contaminaba la senal de TPE.
    - Anade min_split_gain (analogo a gamma de XGB) para simetrizar la
      regularizacion entre backends.
    - Anade feature_fraction_bynode (analogo a colsample_bynode).
    - learning_rate piso 5e-3 -> 1e-2.
    - Afloja min_child_samples 50-150 -> 20-100 (FUNDO chico no debe
      ahogarse).
    Notas: el objective='quantile' alpha=0.5 (ver model_lgb.py) cambia la
    escala del Hessian; los rangos de min_child_weight no son directamente
    comparables a un LGB con objective='regression'.
    """
    max_depth = trial.suggest_int("regressor__regressor__max_depth", 3, 7)
    # Tope teorico de hojas dado max_depth, capado a 127 por velocidad.
    num_leaves_max = min(2 ** max_depth - 1, 127)
    return {
        "regressor__regressor__n_estimators": trial.suggest_int(
            "regressor__regressor__n_estimators", 200, 1500
        ),
        "regressor__regressor__max_depth": max_depth,
        "regressor__regressor__num_leaves": trial.suggest_int(
            "regressor__regressor__num_leaves", 7, num_leaves_max
        ),
        "regressor__regressor__learning_rate": trial.suggest_float(
            "regressor__regressor__learning_rate", 1e-2, 0.3, log=True
        ),
        "regressor__regressor__subsample": trial.suggest_float(
            "regressor__regressor__subsample", 0.5, 1.0
        ),
        "regressor__regressor__subsample_freq": trial.suggest_int(
            "regressor__regressor__subsample_freq", 1, 7
        ),
        "regressor__regressor__colsample_bytree": trial.suggest_float(
            "regressor__regressor__colsample_bytree", 0.5, 1.0
        ),
        "regressor__regressor__feature_fraction_bynode": trial.suggest_float(
            "regressor__regressor__feature_fraction_bynode", 0.5, 1.0
        ),
        "regressor__regressor__min_child_samples": trial.suggest_int(
            "regressor__regressor__min_child_samples", 20, 100
        ),
        "regressor__regressor__min_split_gain": trial.suggest_float(
            "regressor__regressor__min_split_gain", 1e-3, 1.0, log=True
        ),
        "regressor__regressor__reg_alpha": trial.suggest_float(
            "regressor__regressor__reg_alpha", 1e-3, 10.0, log=True
        ),
        "regressor__regressor__reg_lambda": trial.suggest_float(
            "regressor__regressor__reg_lambda", 0.5, 10.0, log=True
        ),
    }


# ---------------------------------------------------------------------------
# Registro: backend -> funcion de search space
# ---------------------------------------------------------------------------

SEARCH_SPACE_REGISTRY: Dict[str, callable] = {
    "xgb": suggest_xgb_params,
    "lgb": suggest_lgb_params,
}


def suggest_full_params(trial: optuna.Trial, model_type: str) -> Dict[str, object]:
    """Concatena search space del preprocesador + del modelo elegido."""
    if model_type not in SEARCH_SPACE_REGISTRY:
        raise ValueError(
            f"model_type '{model_type}' no soportado. "
            f"Disponibles: {list(SEARCH_SPACE_REGISTRY)}"
        )
    return {
        **suggest_preprocessor_params(trial),
        **SEARCH_SPACE_REGISTRY[model_type](trial),
    }

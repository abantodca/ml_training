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
    """Search space anti-overfitting (rev. 5 - cerrar gap remanente):
    XGB ya tenia gap=+0.135 (el sano). Rev anterior dejaba alpha=0.017 y
    gamma=0.10 (Optuna eligio 'casi sin uso') -> palancas desperdiciadas.
    Esta rev. sube los pisos para forzar uso real de esas palancas y
    apunta a gap ~0.10-0.12 sin sacrificar MAE_test.
    - max_depth: 3-7 -> 3-6 (Optuna eligio 6, no 7; bajamos techo 1 para
      cerrar el espacio sin podar la zona util).
    - min_child_weight: 3-30 -> 5-50 (sube piso, evita splits de hoja chica).
    - reg_alpha: 1e-3 a 10 -> 0.1 a 20 (sube piso 100x; mata el 'sin alpha').
    - gamma: 0.1 a 10 -> 0.5 a 15 (sube piso 5x; obliga mejora minima
      mas exigente para crear un split).
    NO se tocan colsample_* (Optuna eligio 0.61-0.78, zona buena).
    """
    return {
        "regressor__regressor__n_estimators": trial.suggest_int(
            "regressor__regressor__n_estimators", 200, 1500
        ),
        "regressor__regressor__max_depth": trial.suggest_int(
            "regressor__regressor__max_depth", 3, 6
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
            "regressor__regressor__min_child_weight", 5.0, 50.0, log=True
        ),
        "regressor__regressor__gamma": trial.suggest_float(
            "regressor__regressor__gamma", 0.5, 15.0, log=True
        ),
        "regressor__regressor__max_delta_step": trial.suggest_int(
            "regressor__regressor__max_delta_step", 0, 7
        ),
        "regressor__regressor__reg_alpha": trial.suggest_float(
            "regressor__regressor__reg_alpha", 0.1, 20.0, log=True
        ),
        "regressor__regressor__reg_lambda": trial.suggest_float(
            "regressor__regressor__reg_lambda", 1.0, 10.0, log=True
        ),
    }


# ---------------------------------------------------------------------------
# LightGBM
# ---------------------------------------------------------------------------


def suggest_lgb_params(trial: optuna.Trial) -> Dict[str, object]:
    """Search space anti-overfitting (rev. 5 - cierre de gap):
    Rev anterior dejaba a Optuna elegir max_depth=7 + num_leaves=88 +
    min_child_samples=29, lo que producia gap=+0.24. Esta rev. fuerza
    arboles mas chicos y hojas con mas filas para cerrar el gap.
    - max_depth: 3-7 -> 3-5 (techo bajo, ya no permite arboles muy
      profundos). Al techo viejo Optuna chocaba el limite.
    - num_leaves: cap 127 -> 31 (con 16 estratos y 10k filas, 31 hojas
      ~ 322 filas/hoja en promedio, impide hojas hiper-especificas).
    - min_child_samples: 20-100 -> 50-200 (sube piso 2.5x; no permite
      hojas con menos de 50 cosechas, mata memorizacion de outliers).
    - reg_alpha: 1e-3 a 10 -> 0.5 a 20 (sube piso 500x; ya no existe
      el trial 'casi sin alpha' que Optuna podia elegir).
    - reg_lambda: 0.5 a 10 -> 2.0 a 20 (sube piso 4x).
    - min_split_gain: 1e-3 a 1 -> 0.01 a 5 (sube piso 10x; mas exigente
      con la mejora minima para crear un split).
    Notas: el objective='quantile' alpha=0.5 (ver model_lgb.py) cambia la
    escala del Hessian; los rangos de min_child_weight no son directamente
    comparables a un LGB con objective='regression'.
    """
    max_depth = trial.suggest_int("regressor__regressor__max_depth", 3, 5)
    # Cap a 31 hojas: con 10k filas y 16 estratos, ~322 filas/hoja medio.
    num_leaves_max = min(2 ** max_depth - 1, 31)
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
            "regressor__regressor__min_child_samples", 50, 200
        ),
        "regressor__regressor__min_split_gain": trial.suggest_float(
            "regressor__regressor__min_split_gain", 0.01, 5.0, log=True
        ),
        "regressor__regressor__reg_alpha": trial.suggest_float(
            "regressor__regressor__reg_alpha", 0.5, 20.0, log=True
        ),
        "regressor__regressor__reg_lambda": trial.suggest_float(
            "regressor__regressor__reg_lambda", 2.0, 20.0, log=True
        ),
    }


# ---------------------------------------------------------------------------
# Meta-learners (capa de stacking)
# ---------------------------------------------------------------------------
#
# Estos search spaces son INDEPENDIENTES del backend del base. Se mantienen
# aqui para que un futuro tuning del meta tenga la misma API (`suggest_*` +
# registry) que el backend. En la implementacion ACTUAL del stacking
# (`StackedRegressor`) los hiperparametros del GAM se leen de `config.py`
# (defaults) en lugar de tunearse junto al base; tunearlos en el mismo
# trial multiplicaria el costo (cada trial fitea base+meta). Cuando se
# decida tunear el GAM, el caller debe combinar este dict con el del base
# usando una rama separada del optuna study (no un solo objective monolitico).


def suggest_gam_meta_params(trial: optuna.Trial) -> Dict[str, object]:
    """Search space para el LinearGAM meta-learner.

    Las claves NO usan el prefijo `regressor__regressor__` porque el GAM
    no es un step del Pipeline sklearn: vive como atributo de
    `StackedRegressor`. El caller mapea estos valores a los kwargs del
    constructor de `StackedRegressor` directamente.
    """
    return {
        "gam_n_splines": trial.suggest_int("gam_n_splines", 8, 25),
        "gam_lam": trial.suggest_float("gam_lam", 1e-2, 10.0, log=True),
    }


META_SEARCH_SPACE_REGISTRY: Dict[str, callable] = {
    "gam": suggest_gam_meta_params,
}


# ---------------------------------------------------------------------------
# `suggest_full_params`: combina preprocesador + backend.
#
# El registry de backends vive en `step_04_train/registry.py` (single source
# of truth para factory + search_space). Aqui lo importamos LAZY para evitar
# import circular: registry.py importa `suggest_xgb_params` y
# `suggest_lgb_params` de este modulo.
# ---------------------------------------------------------------------------


def suggest_full_params(trial: optuna.Trial, model_type: str) -> Dict[str, object]:
    """Concatena search space del preprocesador + del modelo elegido."""
    from src.step_04_train.registry import get_backend  # lazy: rompe ciclo

    backend = get_backend(model_type)
    return {
        **suggest_preprocessor_params(trial),
        **backend.search_space(trial),
    }

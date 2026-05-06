"""Espacios de busqueda Optuna por backend.

Aislar los espacios aqui (en vez de mezclarlos con el bucle Nested CV)
permite:
    - Anadir un nuevo backend (ngboost, tabnet, ...) editando UN solo
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
    """Search space anti-overfitting (rev. 6.1 - simplificacion + anti-gap):
    Rev. 6 ya endurecio max_depth, reg_lambda, gamma, min_child_weight para
    pasar el quality gate. Rev. 6.1 simplifica el search eliminando palancas
    redundantes que Optuna combinaba en `feature_fraction` efectivo muy bajo
    (run anterior: bytree=0.760 * bylevel=0.762 * bynode=0.668 = 0.39 features
    efectivas en cualquier nodo -> hiper-especializacion -> overfit por otra
    via). Ademas baja n_estimators upper para no perder trials en arboles
    extra que sin early stopping no aportan:
    - colsample_bylevel REMOVIDO (queda fijo en default 1.0). Multiplicaba
      con bytree y producia agresividad combinada.
    - colsample_bynode REMOVIDO (idem).
    - max_delta_step REMOVIDO. Marginal en regresion MAE; gastaba trials.
    - n_estimators upper: 1500 -> 1000. Run anterior eligio 1436 (top); con
      learning_rate ~0.05 y arboles regularizados, despues de 800 arboles el
      gradiente es ~0 y los extras solo memorizan. Sin early stopping en
      el fit el modelo entrena los 1500 igual.
    Mantenido sin cambio respecto a rev. 6: max_depth 3-5, min_child_weight
    10-50, gamma 1.5-15, reg_lambda 3-15, reg_alpha 0.1-20 (log).

    Rev. 6.2 (2026-05-05) anade `grow_policy` con max_leaves condicional:
    - `grow_policy='depthwise'` (default): split todos los nodos del mismo
      nivel antes de bajar. Comportamiento actual.
    - `grow_policy='lossguide'`: split el nodo con mayor gain (Newton-style,
      similar a LGB num_leaves). Requiere acotar con `max_leaves`. Genera
      arboles asimetricos -> mas diversidad ante seeds distintas.
    - `max_leaves` en lossguide: rango 8-32 (espejo conservador de LGB
      num_leaves cap=18). Cuando depthwise, NO se tunea (XGB lo ignora).
    """
    grow_policy = trial.suggest_categorical(
        "regressor__regressor__grow_policy", ["depthwise", "lossguide"]
    )
    params = {
        "regressor__regressor__n_estimators": trial.suggest_int(
            "regressor__regressor__n_estimators", 200, 1000
        ),
        "regressor__regressor__max_depth": trial.suggest_int(
            "regressor__regressor__max_depth", 3, 5
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
        "regressor__regressor__min_child_weight": trial.suggest_float(
            "regressor__regressor__min_child_weight", 10.0, 50.0, log=True
        ),
        "regressor__regressor__gamma": trial.suggest_float(
            "regressor__regressor__gamma", 1.5, 15.0, log=True
        ),
        "regressor__regressor__reg_alpha": trial.suggest_float(
            "regressor__regressor__reg_alpha", 0.1, 20.0, log=True
        ),
        "regressor__regressor__reg_lambda": trial.suggest_float(
            "regressor__regressor__reg_lambda", 3.0, 15.0, log=True
        ),
        "regressor__regressor__grow_policy": grow_policy,
    }
    if grow_policy == "lossguide":
        params["regressor__regressor__max_leaves"] = trial.suggest_int(
            "regressor__regressor__max_leaves", 8, 32
        )
    return params


# ---------------------------------------------------------------------------
# LightGBM
# ---------------------------------------------------------------------------


def suggest_lgb_params(trial: optuna.Trial) -> Dict[str, object]:
    """Search space anti-overfitting (rev. 7.2 - simplificacion + anti-gap):
    Rev. 7.1 capo num_leaves a 18 absoluto y subio min_child_samples / reg_lambda.
    Rev. 7.2 simplifica eliminando palancas redundantes que combinadas producian
    feature_fraction efectivo muy bajo (run anterior: bytree=0.747 * bynode=0.539
    = 0.40 features efectivas -> hiper-especializacion -> overfit por otra via),
    y elimina subsample_freq tunable (movido a model_lgb.py como fijo en 1):
    - feature_fraction_bynode REMOVIDO (queda fijo en default 1.0). Multiplicaba
      con colsample_bytree y producia agresividad combinada.
    - subsample_freq REMOVIDO del search; ahora fijo a 1 en model_lgb.py.
      Antes podia variar 1-7; valores altos anulaban el bagging porque solo
      1/N arboles veia un sample (resto entrenaba con dataset completo).
    - n_estimators upper: 1500 -> 1000. Run anterior eligio 1165 (cerca top);
      con LR ~0.05 y arboles regularizados, despues de 800 arboles el gradiente
      es ~0 y los extras solo memorizan. Sin early stopping no se cortan.
    Mantenido respecto a rev. 7.1: max_depth 3-5, num_leaves cap absoluto 18,
    subsample 0.5-0.80, min_child_samples 60-250, reg_lambda 5-25.
    Notas: objective='regression_l1' (ver model_lgb.py) -> MAE nativo.

    Rev. 7.3 (2026-05-05) anade dos palancas anti-gap ortogonales a las
    actuales (no se duplican con depth/min_child_samples/reg_lambda):
    - `extra_trees` True/False: random splits estilo ExtraTrees. Cuando True,
      el split point se elige random dentro del rango (no greedy). Reduce
      capacidad efectiva del arbol y la varianza entre arboles -> menor
      overfit. Costo: cada arbol individual es peor, pero el ensemble suma
      diversidad. Optuna decide si ayuda.
    - `path_smooth` 0-2: regularizacion bayesiana sobre splits con poco
      soporte (smooth = 0 desactiva). Penaliza splits con n chico de un
      lado, lo que combate el overfit por ruido en hojas marginales.
      Rango 0-2 (oficial: >0 typical 1-2).
    """
    max_depth = trial.suggest_int("regressor__regressor__max_depth", 3, 5)
    # Cap absoluto 18 sobre la formula 2^depth - 8 (era cap 24 en depth=5).
    # depth=5 -> min(18, 24) = 18. depth=4 -> min(18, 8) = 8. depth=3 -> 7.
    # Floor=7 para no degenerar.
    num_leaves_max = min(18, max(7, 2 ** max_depth - 8))
    return {
        "regressor__regressor__n_estimators": trial.suggest_int(
            "regressor__regressor__n_estimators", 200, 1000
        ),
        "regressor__regressor__max_depth": max_depth,
        "regressor__regressor__num_leaves": trial.suggest_int(
            "regressor__regressor__num_leaves", 7, num_leaves_max
        ),
        "regressor__regressor__learning_rate": trial.suggest_float(
            "regressor__regressor__learning_rate", 1e-2, 0.3, log=True
        ),
        "regressor__regressor__subsample": trial.suggest_float(
            "regressor__regressor__subsample", 0.5, 0.80
        ),
        "regressor__regressor__colsample_bytree": trial.suggest_float(
            "regressor__regressor__colsample_bytree", 0.5, 1.0
        ),
        "regressor__regressor__min_child_samples": trial.suggest_int(
            "regressor__regressor__min_child_samples", 60, 250
        ),
        "regressor__regressor__min_split_gain": trial.suggest_float(
            "regressor__regressor__min_split_gain", 0.01, 5.0, log=True
        ),
        "regressor__regressor__reg_alpha": trial.suggest_float(
            "regressor__regressor__reg_alpha", 0.5, 20.0, log=True
        ),
        "regressor__regressor__reg_lambda": trial.suggest_float(
            "regressor__regressor__reg_lambda", 5.0, 25.0, log=True
        ),
        "regressor__regressor__extra_trees": trial.suggest_categorical(
            "regressor__regressor__extra_trees", [False, True]
        ),
        "regressor__regressor__path_smooth": trial.suggest_float(
            "regressor__regressor__path_smooth", 0.0, 2.0
        ),
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

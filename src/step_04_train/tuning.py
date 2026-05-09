"""Tuning bayesiano (Optuna) con Nested Cross-Validation.

Diseno:
- Outer CV    : estima el error de generalizacion del PROCEDIMIENTO completo
                (preprocesamiento + tuning + entrenamiento), no de un modelo
                concreto.
- Inner CV    : selecciona los mejores hiperparametros DENTRO de cada outer
                fold con un sampler TPE multivariado de Optuna.
- Final tune  : ronda extra de optimizacion sobre TODO el dataset (suele ser
                mas corta que las del nested CV via `final_trials`) y refit
                del pipeline que se promueve a produccion.

El espacio de busqueda incluye TANTO el modelo como el preprocesador
(`imputer__n_neighbors`, `outliers__factor`, `outliers__method`), de modo
que Optuna tunea el pipeline completo.

Acumulamos predicciones out-of-fold (OOF) durante el outer CV: son
predicciones honestas (cada fila predicha por un modelo que NO la vio en
entrenamiento) y se usan para construir los graficos del reporte gerencial.
"""
from __future__ import annotations

import logging
import time
import warnings
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np
import optuna
import pandas as pd
from optuna.exceptions import ExperimentalWarning
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import KFold, StratifiedKFold
from sklearn.pipeline import Pipeline

warnings.filterwarnings("ignore", category=ExperimentalWarning)

from src.config import (
    INNER_CV_FOLDS,
    OOF_ENSEMBLE_K,
    OUTER_CV_FOLDS,
    RANDOM_STATE,
)
from src.step_04_train.oof_ensemble import OOFEnsembleRegressor
from src.step_04_train.registry import get_backend
from src.step_04_train.sample_weights import compute_sample_weights
from src.step_04_train.search_spaces import suggest_full_params
from src.utils.sklearn_helpers import (
    fit_with_optional_sample_weight,
    index_or_none,
)

# Logger inerte hasta que el caller configure handlers (idem data_loader).
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Factory: delega al BACKEND_REGISTRY (single source of truth)
# ---------------------------------------------------------------------------


def _build_model(model_type: str):
    """Construye el regressor envuelto (TTR + base) para `model_type`."""
    return get_backend(model_type).factory()


# ---------------------------------------------------------------------------
# Optuna study factory + objective
# ---------------------------------------------------------------------------


def _make_study(seed: int) -> optuna.Study:
    """TPE multivariado. Sin pruner: cada trial devuelve UN solo score
    (CV ya hecho), no hay valores intermedios que prunir."""
    sampler = optuna.samplers.TPESampler(
        seed=seed, multivariate=True, warn_independent_sampling=False
    )
    return optuna.create_study(direction="minimize", sampler=sampler)


def _build_pipeline(preprocessor: Pipeline, model_type: str) -> Pipeline:
    return Pipeline(
        steps=[
            ("preprocessor", preprocessor),
            ("regressor", _build_model(model_type)),
        ]
    )


def _build_strat_label(
    X: pd.DataFrame, min_count: int,
) -> Tuple[Optional[pd.Series], str]:
    """Etiqueta de estratificacion ADAPTATIVA por variedad.

    Cascada de estrategias (mas especifica primero):
        1. `FUNDO_FORMATO` compuesto, con clases n<min_count -> 'RARE'.
        2. `FUNDO` solo (idem).
        3. `FORMATO` solo (idem).
        4. None -> caller cae a `KFold` sin estratificar.

    En cada nivel se valida que tras colapsar:
        - hay >=2 clases distintas (sin variabilidad no se puede stratify),
        - cada clase final tiene n>=min_count (requisito de StratifiedKFold).

    Asi una variedad con 4x4 categoricas y desbalance moderado entra por la
    estrategia compuesta; una variedad con 1 solo FUNDO entra por FORMATO; y
    una con 1 FUNDO y 1 FORMATO degenera a KFold sin tropezar.

    Devuelve (label, strategy_name) para que el caller logue la decision.
    """
    candidates: list[tuple[str, pd.Series]] = []
    if "FUNDO" in X.columns and "FORMATO" in X.columns:
        candidates.append(
            ("FUNDO_FORMATO",
             X["FUNDO"].astype(str) + "_" + X["FORMATO"].astype(str))
        )
    if "FUNDO" in X.columns:
        candidates.append(("FUNDO", X["FUNDO"].astype(str)))
    if "FORMATO" in X.columns:
        candidates.append(("FORMATO", X["FORMATO"].astype(str)))

    for name, label in candidates:
        counts = label.value_counts()
        rare = counts[counts < min_count].index
        if len(rare) > 0:
            label = label.where(~label.isin(rare), other="RARE")
        final_counts = label.value_counts()
        if len(final_counts) >= 2 and (final_counts >= min_count).all():
            return label, name
    return None, "none"


def _objective(
    trial: optuna.Trial,
    X_train: pd.DataFrame,
    y_train: pd.Series,
    preprocessor: Pipeline,
    inner_cv,
    model_type: str,
    sample_weights_train: Optional[np.ndarray] = None,
    strat_label_train: Optional[pd.Series] = None,
) -> float:
    """Optuna objective: MAE promedio del inner CV con sample_weight por fold.

    Inner CV manual (no `cross_val_score`) porque sklearn no splitea
    sample_weight por fold y lo trataria como un kwarg estatico. Cuando
    `sample_weights_train` es None se pasa sample_weight=None al fit
    (degeneracion natural -> equivalente a `cross_val_score` sin pesos,
    pero sin la rama paralela que duplicaria codigo).

    `inner_cv` puede ser `KFold` o `StratifiedKFold`. Si es Stratified, hay
    que pasarle el `strat_label_train` (alineado con X_train por posicion).
    KFold ignora el segundo argumento, asi que llamamos `.split(X, y_label)`
    siempre y dejamos que sklearn decida.

    Nota: en produccion siempre va con weights (use_sample_weights=True
    es el default y compensa el sesgo 'regresion a la media' de los
    arboles).
    """
    params = suggest_full_params(trial, model_type)
    scores: list[float] = []
    for tr_i, te_i in inner_cv.split(X_train, strat_label_train):
        Xt = X_train.iloc[tr_i]
        Xv = X_train.iloc[te_i]
        yt = y_train.iloc[tr_i]
        yv = y_train.iloc[te_i]
        pipe_local = _build_pipeline(preprocessor, model_type)
        pipe_local.set_params(**params)
        sw_fold = index_or_none(sample_weights_train, tr_i)
        fit_with_optional_sample_weight(pipe_local, Xt, yt, sample_weight=sw_fold)
        pred = pipe_local.predict(Xv)
        scores.append(float(mean_absolute_error(yv, pred)))
    return float(np.mean(scores))


# ---------------------------------------------------------------------------
# Nested CV
# ---------------------------------------------------------------------------


def _format_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


# ---------------------------------------------------------------------------
# Helpers privados de Nested CV (extraidos de perform_nested_cv para que el
# orquestador quede como lectura lineal de ~50 lineas).
# ---------------------------------------------------------------------------


@dataclass
class _OuterFoldResults:
    """Acumulado del outer CV loop. Mutable por construccion incremental
    (append por fold). El orquestador lo agrega a `nested_metrics` al final.
    """

    mae_test: list[float]
    mae_train: list[float]
    gap: list[float]
    r2: list[float]
    best_params: list[Dict[str, object]]
    oof_pred: np.ndarray
    oof_fold: np.ndarray


def _build_cv_splitters(
    X: pd.DataFrame,
    outer_folds: int,
    inner_folds: int,
    random_state: int,
):
    """Construye outer/inner CV con stratification adaptativa.

    Devuelve `(outer_cv, inner_cv, strat_label, strat_strategy)`. Si la
    variedad no soporta stratification (1 FUNDO + 1 FORMATO o columnas
    ausentes), `strat_label` es None y el caller cae a `KFold` normal.

    `min_count`: tras el outer split, el train fold contiene ~N*(outer-1)/outer
    miembros de cada clase. El inner StratifiedKFold necesita >= inner_folds
    en cada clase, asi que el minimo seguro a nivel de dataset completo es
    `ceil(inner * outer / (outer-1))`. Se toma el max con `outer_folds` para
    no quedar por debajo del requisito del propio outer.
    """
    import math
    strat_min_count = max(
        outer_folds,
        math.ceil(inner_folds * outer_folds / max(outer_folds - 1, 1)),
    )
    strat_label, strat_strategy = _build_strat_label(X, min_count=strat_min_count)
    splitter_cls = StratifiedKFold if strat_label is not None else KFold
    outer_cv = splitter_cls(n_splits=outer_folds, shuffle=True, random_state=random_state)
    inner_cv = splitter_cls(n_splits=inner_folds, shuffle=True, random_state=random_state)
    return outer_cv, inner_cv, strat_label, strat_strategy


def _maybe_sample_weights(
    y: pd.Series, use_sample_weights: bool, logger,
) -> Optional[np.ndarray]:
    """Computa sample_weights por decil del target o devuelve None."""
    if not use_sample_weights:
        return None
    sw = compute_sample_weights(y, n_bins=10)
    logger.info(
        f"Sample weights ON | n_bins=10 | "
        f"min={sw.min():.3f} max={sw.max():.3f} mean={sw.mean():.3f}"
    )
    return sw


def _run_outer_cv_loop(
    *,
    X: pd.DataFrame,
    y: pd.Series,
    preprocessor: Pipeline,
    model_type: str,
    outer_cv,
    inner_cv,
    strat_label: Optional[pd.Series],
    sample_weights: Optional[np.ndarray],
    n_trials: int,
    final_trials: int,
    skip_final_tuning: bool,
    outer_folds: int,
    random_state: int,
    t0: float,
    logger,
) -> _OuterFoldResults:
    """Itera outer folds: tune Optuna inner + refit + eval test/train.

    Acumula metricas por fold y predicciones OOF. El refit por fold es
    necesario para evaluar gap (MAE_test - MAE_train) honestamente.
    """
    n = len(y)
    res = _OuterFoldResults(
        mae_test=[], mae_train=[], gap=[], r2=[], best_params=[],
        oof_pred=np.full(n, np.nan, dtype=float),
        oof_fold=np.full(n, -1, dtype=int),
    )
    for fold_idx, (train_idx, test_idx) in enumerate(
        outer_cv.split(X, strat_label), start=1,
    ):
        fold_t0 = time.perf_counter()
        logger.info(f"Outer fold {fold_idx}/{outer_folds} | tuning + eval")

        X_tr, X_te = X.iloc[train_idx], X.iloc[test_idx]
        y_tr, y_te = y.iloc[train_idx], y.iloc[test_idx]
        sw_tr = index_or_none(sample_weights, train_idx)
        # strat_label es pd.Series: requiere .iloc, no encaja en index_or_none.
        strat_tr = strat_label.iloc[train_idx] if strat_label is not None else None

        study = _make_study(random_state + fold_idx)
        study.optimize(
            lambda trial: _objective(
                trial, X_tr, y_tr, preprocessor, inner_cv, model_type,
                sample_weights_train=sw_tr,
                strat_label_train=strat_tr,
            ),
            n_trials=n_trials,
            show_progress_bar=False,
            gc_after_trial=True,
        )

        best_pipeline = _build_pipeline(preprocessor, model_type)
        best_pipeline.set_params(**study.best_params)
        fit_with_optional_sample_weight(best_pipeline, X_tr, y_tr, sample_weight=sw_tr)

        y_pred_te = best_pipeline.predict(X_te)
        mae_test = float(mean_absolute_error(y_te, y_pred_te))
        r2_test = float(r2_score(y_te, y_pred_te))
        y_pred_tr = best_pipeline.predict(X_tr)
        mae_train = float(mean_absolute_error(y_tr, y_pred_tr))

        res.mae_test.append(mae_test)
        res.mae_train.append(mae_train)
        res.gap.append(mae_test - mae_train)
        res.r2.append(r2_test)
        res.best_params.append(dict(study.best_params))
        res.oof_pred[test_idx] = y_pred_te
        res.oof_fold[test_idx] = fold_idx

        fold_dt = time.perf_counter() - fold_t0
        elapsed = time.perf_counter() - t0
        eta = (elapsed / fold_idx) * (outer_folds - fold_idx) + (
            0 if skip_final_tuning else (final_trials / n_trials) * (elapsed / fold_idx)
        )
        logger.info(
            f"Fold {fold_idx} | MAE_test={mae_test:.4f} | MAE_train={mae_train:.4f} | "
            f"gap={mae_test - mae_train:+.4f} | R2={r2_test:.4f} | "
            f"dt={_format_eta(fold_dt)} | eta_resto={_format_eta(eta)}"
        )
    return res


def _aggregate_nested_metrics(res: _OuterFoldResults) -> Dict[str, float]:
    """Agrega listas por-fold en el dict que consume el HTML / business audit."""
    return {
        # backward-compatible (lo que ya leia el HTML)
        "nested_cv_mae_mean": float(np.mean(res.mae_test)),
        "nested_cv_mae_std": float(np.std(res.mae_test)),
        "nested_cv_r2_mean": float(np.mean(res.r2)),
        "nested_cv_r2_std": float(np.std(res.r2)),
        # detector de overfitting
        "nested_cv_mae_train_mean": float(np.mean(res.mae_train)),
        "nested_cv_mae_train_std": float(np.std(res.mae_train)),
        "nested_cv_gap_mean": float(np.mean(res.gap)),
        "nested_cv_gap_std": float(np.std(res.gap)),
    }


def _pick_final_params(
    *,
    fold_results: _OuterFoldResults,
    X: pd.DataFrame,
    y: pd.Series,
    preprocessor: Pipeline,
    inner_cv,
    model_type: str,
    sample_weights: Optional[np.ndarray],
    strat_label: Optional[pd.Series],
    final_trials: int,
    skip_final_tuning: bool,
    random_state: int,
    logger,
) -> Dict[str, object]:
    """Devuelve los params para el refit final.

    Dos modos:
      - `skip_final_tuning=True`: argmin sobre los outer folds (rapido).
      - `False` (default): ronda extra de Optuna sobre TODO el dataset.
    """
    if skip_final_tuning:
        best_idx = int(np.argmin(fold_results.mae_test))
        logger.info(
            f"Saltando ronda final | usando best_params del fold #{best_idx + 1} "
            f"(MAE_test={fold_results.mae_test[best_idx]:.4f})"
        )
        return fold_results.best_params[best_idx]

    logger.info(f"Ronda final | trials={final_trials} sobre dataset completo...")
    final_study = _make_study(random_state)
    final_study.optimize(
        lambda trial: _objective(
            trial, X, y, preprocessor, inner_cv, model_type,
            sample_weights_train=sample_weights,
            strat_label_train=strat_label,
        ),
        n_trials=final_trials,
        show_progress_bar=False,
        gc_after_trial=True,
    )
    return final_study.best_params


def _fit_final_ensemble(
    *,
    preprocessor: Pipeline,
    model_type: str,
    best_params: Dict[str, object],
    X: pd.DataFrame,
    y: pd.Series,
    sample_weights: Optional[np.ndarray],
    random_state: int,
    t0: float,
    logger,
) -> OOFEnsembleRegressor:
    """Wrap del pipeline tuneado en OOFEnsembleRegressor + fit sobre todo X."""
    base_pipeline = _build_pipeline(preprocessor, model_type)
    base_pipeline.set_params(**best_params)
    ensemble = OOFEnsembleRegressor(
        base_pipeline=base_pipeline,
        n_models=OOF_ENSEMBLE_K,
        random_state=random_state,
    )
    ensemble.fit(X, y, sample_weight=sample_weights)
    logger.info(
        f"Pipeline final entrenado | K={OOF_ENSEMBLE_K} pipelines promediados | "
        f"tiempo_total={_format_eta(time.perf_counter() - t0)} | "
        f"best_params={best_params}"
    )
    return ensemble


def perform_nested_cv(
    X: pd.DataFrame,
    y: pd.Series,
    preprocessor: Pipeline,
    n_trials: int = 30,
    final_trials: Optional[int] = None,
    model_type: str = "xgb",
    outer_folds: Optional[int] = None,
    inner_folds: Optional[int] = None,
    random_state: int = RANDOM_STATE,
    skip_final_tuning: bool = False,
    inner_cv_n_jobs: int = -1,
    use_sample_weights: bool = True,
    logger=logger,
) -> Tuple[Pipeline, Dict[str, object], Dict[str, float], Dict[str, np.ndarray]]:
    """Orquestador thin de Nested CV. La logica vive en helpers privados.

    Parametros
    ----------
    n_trials : trials de Optuna POR outer fold.
    final_trials : trials de la ronda extra sobre el dataset completo.
                   Si es None se usa el mismo `n_trials`.
    skip_final_tuning : si True, omite la ronda final y refitea con los
                        mejores parametros del MEJOR outer fold (argmin MAE
                        test). Ahorra ~1/(outer_folds+1) del tiempo total.
    inner_cv_n_jobs : VESTIGIAL. Ya no aplica: el inner CV se hace manual
                      (fold a fold) para soportar sample_weight. Aceptamos
                      el flag para no romper la CLI/settings. La paralelizacion
                      real ahora es por variedad (`--parallel-varieties`).

    Returns
    -------
    final_pipeline   : `OOFEnsembleRegressor` con K pipelines refiteados
                        sobre folds del KFold (K = `config.OOF_ENSEMBLE_K`).
    best_params      : dict con los hiperparametros del modelo de produccion.
    nested_metrics   : dict con MAE/R2 mean y std (test, train, gap).
    oof              : dict con `y_true`, `y_pred` y `fold_id`.
    """
    outer_folds = outer_folds or OUTER_CV_FOLDS
    inner_folds = inner_folds or INNER_CV_FOLDS
    final_trials = final_trials if final_trials is not None else n_trials

    outer_cv, inner_cv, strat_label, strat_strategy = _build_cv_splitters(
        X, outer_folds, inner_folds, random_state,
    )

    total_trials = outer_folds * n_trials + (0 if skip_final_tuning else final_trials)
    logger.info(
        f"Nested CV inicio | model={model_type} | outer={outer_folds} | "
        f"inner={inner_folds} | trials/fold={n_trials} | "
        f"final_trials={0 if skip_final_tuning else final_trials} | "
        f"trials_total={total_trials}"
    )
    if strat_label is not None:
        logger.info(
            f"CV stratified by {strat_strategy} | "
            f"n_estratos={strat_label.nunique()} | "
            f"min_n_per_strato={int(strat_label.value_counts().min())}"
        )
    else:
        logger.info(
            "CV NO estratificado (variedad sin variabilidad util en "
            "FUNDO/FORMATO; KFold normal)"
        )

    sample_weights = _maybe_sample_weights(y, use_sample_weights, logger)
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    t0 = time.perf_counter()

    fold_results = _run_outer_cv_loop(
        X=X, y=y, preprocessor=preprocessor, model_type=model_type,
        outer_cv=outer_cv, inner_cv=inner_cv, strat_label=strat_label,
        sample_weights=sample_weights,
        n_trials=n_trials, final_trials=final_trials,
        skip_final_tuning=skip_final_tuning,
        outer_folds=outer_folds, random_state=random_state,
        t0=t0, logger=logger,
    )
    nested_metrics = _aggregate_nested_metrics(fold_results)
    logger.info(
        f"Nested CV resultado | MAE_test={nested_metrics['nested_cv_mae_mean']:.4f} "
        f"+/- {nested_metrics['nested_cv_mae_std']:.4f} | "
        f"MAE_train={nested_metrics['nested_cv_mae_train_mean']:.4f} | "
        f"gap={nested_metrics['nested_cv_gap_mean']:+.4f} | "
        f"R2={nested_metrics['nested_cv_r2_mean']:.4f}"
    )

    best_params = _pick_final_params(
        fold_results=fold_results, X=X, y=y, preprocessor=preprocessor,
        inner_cv=inner_cv, model_type=model_type,
        sample_weights=sample_weights, strat_label=strat_label,
        final_trials=final_trials, skip_final_tuning=skip_final_tuning,
        random_state=random_state, logger=logger,
    )
    final_pipeline = _fit_final_ensemble(
        preprocessor=preprocessor, model_type=model_type, best_params=best_params,
        X=X, y=y, sample_weights=sample_weights, random_state=random_state,
        t0=t0, logger=logger,
    )
    oof = {
        "y_true": np.asarray(y, dtype=float),
        "y_pred": fold_results.oof_pred,
        "fold_id": fold_results.oof_fold,
    }
    return final_pipeline, best_params, nested_metrics, oof

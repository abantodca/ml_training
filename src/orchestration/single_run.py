"""Entrenamiento de UN modelo (xgb|lgb) para UNA variedad.

Devuelve un `ModelResult` listo para `select_champion`. Cada llamada:
  - Abre su propio MLflow run dentro del experimento de la variedad.
  - Corre nested CV + Optuna independiente.
  - Loguea metricas Train/Test/Full (model y business units), params, tags.
  - Persiste pipeline + summary JSON.

NO genera HTML por modelo: el dashboard ejecutivo se construye una sola vez
en `variety_runner` despues de elegir campeon (`Winner_{variedad}.html`).
NO selecciona ni registra el campeon: eso vive en `variety_runner` para
poder elegir entre todos los modelos al final.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from typing import Dict, Optional

import joblib
import mlflow
import numpy as np

from src.config import ARTIFACTS_DIR
from src.pipeline.build_pipeline import create_preprocessing_pipeline
from src.step_01_load.data_loader import load_business_columns, load_data
from src.step_04_train.tuning import perform_nested_cv
from src.step_05_evaluate.champion import ModelResult
from src.step_05_evaluate.metrics import calculate_regression_metrics
from src.step_05_evaluate.stacking_diagnostics import (
    StackingDiagnostics,
    extract_stacking_diagnostics,
)
from src.step_06_track.business_validation import (
    BusinessValidation,
    validate_against_business_unit,
)
from src.step_06_track.mlflow_registry import (
    log_artifact,
    log_business_metrics,
    log_metrics,
    log_params,
    log_pipeline,
    next_run_version,
    set_tags,
)
from src.utils.logger import PrefixAdapter, log_business_audit
from src.utils.sklearn_helpers import dump_json_artifact


def _full_dataset_metrics(
    final_pipeline,
    X,
    y,
    business_validation: BusinessValidation,
    logger=None,
) -> tuple[Dict[str, float], Dict[str, float], Optional[np.ndarray]]:
    """Computa metricas sobre el DATASET COMPLETO (refit + predict all).

    Returns
    -------
    full_metrics_business : KG/JR (unidad de negocio). Vacio si faltan KG/JR/H-EF.
    full_metrics_h        : KG/JR_H (unidad del modelo).
    pred_h_full           : array de predicciones en KG/JR_H sobre todo X.
                            None si la prediccion fallo.

    Nota: in-sample es OPTIMISTA (modelo predice lo que entreno). Se usa
    como sanity check del modelo de produccion y para el panel de
    "Aplicacion Total" del dashboard, NO para decidir despliegue.
    """
    try:
        pred_h_full = np.asarray(final_pipeline.predict(X), dtype=float)
    except Exception:
        if logger is not None:
            logger.warning(
                "full_metrics: final_pipeline.predict(X) fallo; "
                "se omite tarjeta 'Aplicacion Total'", exc_info=True,
            )
        pred_h_full = None

    full_metrics_h: Dict[str, float] = {}
    if pred_h_full is not None:
        full_metrics_h = calculate_regression_metrics(
            np.asarray(y, dtype=float), pred_h_full,
        )

    # KG/JR (business): reusamos las metricas in-sample que ya calcula
    # validate_against_business_unit (refit + predict all + multiplicar por H-EF).
    full_metrics_business = dict(business_validation.metrics_insample or {})

    return full_metrics_business, full_metrics_h, pred_h_full


def _resolve_stacking_meta(settings: dict) -> tuple[str, Optional[str]]:
    """Devuelve (stacking_raw, stacking_normalized).

    `raw` se loguea como param (string '"none"', '"gam"', ...).
    `normalized` se pasa a perform_nested_cv (None | 'gam').
    """
    raw = settings.get("stacking", "none")
    norm = None if (not raw or raw == "none") else str(raw)
    return raw, norm


def _set_initial_run_tags(variety: str, model_type: str, version: int, args) -> None:
    """Tags MLflow basicos al abrir el run. Se invoca DENTRO del start_run."""
    mlflow.set_tag("variety", variety)
    mlflow.set_tag("tuning", args.tuning)
    mlflow.set_tag("model_type", model_type)
    mlflow.set_tag("version", f"v{version}")
    mlflow.set_tag("trained_at", datetime.now().isoformat(timespec="seconds"))


def _log_full_metrics(
    full_metrics_business: Dict[str, float],
    full_metrics_h: Dict[str, float],
) -> None:
    """Loguea metricas full a MLflow con prefijos `full_business_` / `full_model_`.

    Tags resumen `full_business_mape` / `full_business_r2` filtrables en UI.
    """
    if full_metrics_business:
        log_metrics({f"full_business_{k}": v for k, v in full_metrics_business.items()})
        set_tags({
            "full_business_mape": f"{full_metrics_business.get('mape', float('nan')):.2f}",
            "full_business_r2":   f"{full_metrics_business.get('r2',   float('nan')):.4f}",
        })
    if full_metrics_h:
        log_metrics({f"full_model_{k}": v for k, v in full_metrics_h.items()})


def _log_pipeline_with_signature(final_pipeline, X) -> None:
    """log_pipeline con signature inferida desde un sample de X.

    Castea int columns -> float64 SOLO en el sample (no en train data) para
    que la firma sea NaN-safe: el runtime de MLflow promueve int->float si
    encuentra NaN en inferencia y, sin este cast, rompe schema enforcement.
    """
    X_sample = X.head(min(50, len(X))).copy()
    int_cols = X_sample.select_dtypes(include=["integer"]).columns
    if len(int_cols) > 0:
        X_sample[int_cols] = X_sample[int_cols].astype("float64")
    try:
        y_sample = final_pipeline.predict(X_sample)
    except Exception:
        y_sample = None
    log_pipeline(
        final_pipeline, name="model_pipeline",
        X_sample=X_sample, y_sample=y_sample,
    )


def _build_run_summary(
    *,
    variety: str,
    model_type: str,
    run_id: str,
    nested_metrics: Dict[str, float],
    bv_oof_dump: Dict[str, float],
    full_metrics_business: Dict[str, float],
    full_metrics_h: Dict[str, float],
    best_params: Dict[str, object],
    local_pipeline,
    elapsed: float,
) -> dict:
    """Pure data construction: dict serializable del summary del run."""
    return {
        "variety": variety,
        "model_type": model_type,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "mlflow_run_id": run_id,
        "metrics": {k: float(v) for k, v in nested_metrics.items()},
        "business_metrics_oof": bv_oof_dump,
        "full_metrics_business": {k: float(v) for k, v in full_metrics_business.items()},
        "full_metrics_model": {k: float(v) for k, v in full_metrics_h.items()},
        "best_params": {
            k: (float(v) if isinstance(v, (int, float)) else v)
            for k, v in best_params.items()
        },
        "artifacts": {"pipeline": str(local_pipeline)},
        "elapsed_seconds": round(elapsed, 2),
    }


def train_model(
    variety: str,
    model_type: str,
    args: argparse.Namespace,
    settings: dict,
    logger,
) -> ModelResult:
    """Entrena UN modelo (xgb|lgb) para UNA variedad. Devuelve ModelResult."""
    log = PrefixAdapter(logger, prefix=f"[{variety}/{model_type}]")
    logger.info("-" * 78)
    logger.info(f"# {variety} / {model_type}")
    logger.info("-" * 78)

    t0 = time.perf_counter()

    log.info(f"[1/6] Cargando datos | hoja={variety}")
    X, y = load_data(sheet=variety)
    business_cols = load_business_columns(sheet=variety)  # KG/JR + H-EF alineadas con (X,y)

    log.info("[2/6] Construyendo preprocesador...")
    preprocessor = create_preprocessing_pipeline()

    # Run name versionado: el experimento ya identifica la variedad, asi que
    # el run solo necesita decir el modelo y su version (xgb_v1, xgb_v2, ...).
    # `experiment_prefix` viene vacio por default desde config.py -> el
    # experimento es el nombre de la variedad (e.g. "POP").
    experiment_name = f"{args.experiment_prefix}{variety}"
    version = next_run_version(experiment_name, model_type)
    run_name = f"{model_type}_v{version}"
    stacking_meta_raw, stacking_meta = _resolve_stacking_meta(settings)

    with mlflow.start_run(run_name=run_name) as run:
        _set_initial_run_tags(variety, model_type, version, args)
        meta_trials = int(settings.get("meta_trials", 0) or 0)
        log_params({
            "variety": variety,
            "tuning": args.tuning,
            "model_type": model_type,
            "n_trials": settings["n_trials"],
            "final_trials": settings["final_trials"],
            "outer_folds": settings["outer_folds"],
            "inner_folds": settings["inner_folds"],
            "skip_final_tuning": settings["skip_final_tuning"],
            "stacking": stacking_meta_raw,
            "meta_trials": meta_trials,
            "n_rows": int(X.shape[0]),
            "n_features_input": int(X.shape[1]),
        })
        # Tag para filtrar en MLflow UI: stacked vs no-stacked al instante.
        mlflow.set_tag("stacked", "true" if stacking_meta else "false")
        if stacking_meta:
            mlflow.set_tag("meta_model", stacking_meta)
            mlflow.set_tag("meta_tuned", "true" if meta_trials > 0 else "false")

        log.info("[3/6] Nested CV con Optuna...")
        final_pipeline, best_params, nested_metrics, oof = perform_nested_cv(
            X=X, y=y, preprocessor=preprocessor,
            n_trials=settings["n_trials"],
            final_trials=settings["final_trials"],
            model_type=model_type,
            outer_folds=settings["outer_folds"],
            inner_folds=settings["inner_folds"],
            skip_final_tuning=settings["skip_final_tuning"],
            inner_cv_n_jobs=settings.get("inner_cv_n_jobs", -1),
            stacking_meta=stacking_meta,
            meta_trials=meta_trials,
            logger=logger,
        )

        log.info("[4/6] MLflow logging...")
        log_metrics(nested_metrics)
        log_params(best_params)
        set_tags({
            "r2_mean": f"{nested_metrics['nested_cv_r2_mean']:.4f}",
            "mae_test_mean": f"{nested_metrics['nested_cv_mae_mean']:.4f}",
            "mae_train_mean": f"{nested_metrics.get('nested_cv_mae_train_mean', 0):.4f}",
            "overfit_gap": f"{nested_metrics.get('nested_cv_gap_mean', 0):+.4f}",
        })

        # Diagnóstico de stacking (None si el pipeline no es un StackedRegressor).
        # Centralizado en `extract_stacking_diagnostics` para que MLflow,
        # dashboard y Excel consuman exactamente la misma vista.
        stacking_diag: Optional[StackingDiagnostics] = extract_stacking_diagnostics(
            final_pipeline,
        )
        if stacking_diag is not None:
            if stacking_diag.tuned_params:
                log_params({f"meta_{k}": v for k, v in stacking_diag.tuned_params.items()})
            if stacking_diag.tuning_log:
                log_metrics({
                    f"meta_tuning_{k}": v for k, v in stacking_diag.tuning_log.items()
                })
            log_metrics({
                "meta_fallback_mae_base_oof": stacking_diag.mae_base_oof,
                "meta_fallback_mae_meta_oof": stacking_diag.mae_meta_oof,
                "meta_fallback_delta_pct": stacking_diag.delta_pct,
                "meta_fallback_active": float(stacking_diag.active),
            })
            set_tags({
                "meta_active": "true" if stacking_diag.active else "false",
                "meta_delta_pct": f"{stacking_diag.delta_pct:+.2f}",
            })

        # ---- Validacion en unidad de negocio (KG/JR = KG/JR_H * H-EF) ----
        log.info("Validando en unidad de negocio (KG/JR)...")
        business_validation = validate_against_business_unit(
            oof=oof, final_pipeline=final_pipeline,
            X_full=X, business_cols=business_cols,
        )
        log_business_metrics(business_validation)
        log_business_audit(
            logger,
            variety=variety, model_type=model_type, tuning=args.tuning,
            business_validation=business_validation,
            nested_metrics=nested_metrics, best_params=best_params,
            mlflow_run_id=run.info.run_id,
        )

        # ---- Metricas en DATASET COMPLETO (refit + predict all) ----
        # "Aplicacion Total": tarjeta del dashboard ejecutivo. Es la perspectiva
        # del modelo de produccion aplicado a toda la historia disponible.
        full_metrics_business, full_metrics_h, _pred_h_full = _full_dataset_metrics(
            final_pipeline, X, y, business_validation, logger=logger,
        )
        _log_full_metrics(full_metrics_business, full_metrics_h)

        # NOTE: el Excel multi-hoja YA NO se genera aqui. Se genera UNA SOLA
        # vez en `variety_runner` para el modelo CAMPEON, en
        # `reports/Winner_{variety}.xlsx` (junto al HTML del dashboard).
        # Razon: evitar archivos residuales de modelos perdedores.

        # best_params como artifact JSON (precision sin truncado de MLflow params).
        # Path versionado por run_name (xgb_v20, lgb_v3, ...) para que el archivo
        # local de v19 NO sea sobrescrito por v20. MLflow ya tiene historial
        # versionado por run_id, esto agrega trazabilidad fuera de MLflow.
        params_path = dump_json_artifact(
            ARTIFACTS_DIR / f"best_params_{variety}_{run_name}.json",
            best_params,
        )
        log_artifact(params_path, artifact_path="hyperparameters")

        log.info("[5/6] Persistiendo pipeline...")
        _log_pipeline_with_signature(final_pipeline, X)
        local_pipeline = ARTIFACTS_DIR / f"final_pipeline_{variety}_{run_name}.joblib"
        joblib.dump(final_pipeline, local_pipeline)

        elapsed = time.perf_counter() - t0
        bv_oof_dump: Dict[str, float] = {}
        if business_validation and not business_validation.is_empty():
            bv_oof_dump = {
                k: float(v) for k, v in business_validation.metrics_oof.items()
                if isinstance(v, (int, float))
            }
        # Summary local versionado por run_name. Cada run histórico (xgb_v19,
        # xgb_v20, ...) conserva su propio JSON sin sobrescribirse.
        summary = _build_run_summary(
            variety=variety, model_type=model_type, run_id=run.info.run_id,
            nested_metrics=nested_metrics, bv_oof_dump=bv_oof_dump,
            full_metrics_business=full_metrics_business,
            full_metrics_h=full_metrics_h,
            best_params=best_params, local_pipeline=local_pipeline,
            elapsed=elapsed,
        )
        summary_path = dump_json_artifact(
            ARTIFACTS_DIR / f"run_summary_{variety}_{run_name}.json",
            summary,
        )
        log_artifact(summary_path)

        log.info(
            f"[6/6] DONE | "
            f"MAE_test={nested_metrics['nested_cv_mae_mean']:.4f} | "
            f"MAE_train={nested_metrics.get('nested_cv_mae_train_mean', 0):.4f} | "
            f"gap={nested_metrics.get('nested_cv_gap_mean', 0):+.4f} | "
            f"R2={nested_metrics['nested_cv_r2_mean']:.4f} | "
            f"FullMAPE={full_metrics_business.get('mape', float('nan')):.2f}% | "
            f"dt={elapsed:.1f}s"
        )

        result = ModelResult(
            model_type=model_type,
            metrics=dict(nested_metrics),
            best_params=dict(best_params),
            mlflow_run_id=run.info.run_id,
            pipeline_path=str(local_pipeline),
            elapsed_seconds=round(elapsed, 2),
            business_metrics_oof=bv_oof_dump or None,
            full_metrics=full_metrics_business or None,
            business_validation=business_validation,
            full_metrics_h=full_metrics_h or None,
            oof_y_true=oof["y_true"],
            oof_y_pred=oof["y_pred"],
            stacking_diagnostics=stacking_diag,
        )

    # liberar referencias grandes ANTES del cleanup global
    del X, y, preprocessor, final_pipeline, best_params, nested_metrics, oof
    del business_cols
    return result

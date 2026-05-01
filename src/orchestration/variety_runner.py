"""Entrenamiento de UNA variedad: todos los modelos + champion + dashboard.

Logica:
  1. Setea el experimento MLflow dedicado de la variedad (sin timestamp).
  2. Itera modelos llamando `train_model` y acumula `ModelResult`s.
  3. Selecciona campeon con LEX-ORDER (en `champion.select_champion`):
        a. menor |gap| Train-Test (overfitting),
        b. menor MAPE en data total (estabilidad refit + predict all),
        c. menor tiempo (eficiencia ante empate practico).
  4. Renderiza UN dashboard ejecutivo: `reports/Winner_{variety}.html`.
  5. Borra los HTML residuales (per-modelo timestamped) de la variedad.
  6. Tagea el run ganador como `is_champion=true` y el resto como false.
  7. Registra UNICAMENTE el campeon en MLflow Model Registry (si --register).
  8. Persiste un `variety_summary_<NAME>.json` con todo el contexto.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from typing import Optional

import mlflow

from src.config import ARTIFACTS_DIR, REPORTS_DIR
from src.orchestration.cleanup import cleanup_residual_reports, cleanup_state
from src.orchestration.single_run import train_model
from src.step_01_load.data_loader import load_business_columns, load_data
from src.step_05_evaluate.champion import (
    ModelResult,
    champion_summary,
    select_champion,
)
from src.step_05_evaluate.html.winner_dashboard import render_winner_dashboard
from src.step_06_track.business_export import export_business_excel
from src.step_06_track.mlflow_registry import register_model, set_experiment
from src.utils.logger import setup_logging


def train_variety(
    variety: str,
    model_types: list[str],
    args: argparse.Namespace,
    settings: dict,
    logger=None,
) -> dict:
    """Entrena TODOS los modelos para UNA variedad y elige campeon.

    Si `logger` es None, crea uno DEDICADO que escribe a
    `logs/variety_<NAME>.log` (modo paralelo: cada variedad su archivo,
    sin interleaving).
    """
    if logger is None:
        logger = setup_logging(
            name=f"variety.{variety}",
            log_file=f"variety_{variety}.log",
        )

    logger.info("#" * 78)
    logger.info(
        f"# VARIEDAD INICIO: {variety} | modelos={model_types} | pid={os.getpid()}"
    )
    logger.info("#" * 78)

    experiment_name = f"{args.experiment_prefix}{variety}"
    set_experiment(experiment_name)
    logger.info(f"[{variety}] MLflow experiment: '{experiment_name}'")

    t_var = time.perf_counter()
    results: list[ModelResult] = []
    failures: list[str] = []

    for model_type in model_types:
        try:
            results.append(train_model(variety, model_type, args, settings, logger))
        except Exception:
            logger.exception(f"[{variety}/{model_type}] FALLO")
            failures.append(model_type)
        finally:
            cleanup_state(logger, f"post {variety}/{model_type}")

    champion_decision: Optional[dict] = None
    registered_name: Optional[str] = None
    winner_report_path: Optional[str] = None
    winner_excel_path: Optional[str] = None
    if results:
        champion = select_champion(results)
        champion_decision = champion_summary(results, champion)
        logger.info(
            f"[{variety}] CAMPEON: {champion.model_type} | "
            f"|gap|={champion.abs_gap:.4f} | "
            f"MAPE_full={champion.full_mape:.2f}% | "
            f"dt={champion.elapsed_seconds:.1f}s | "
            f"composite_aux={champion.composite_score:.4f}"
        )
        logger.info(f"[{variety}] Decision: {champion_decision.get('justification', '')}")

        # Cargar (X, business_cols) UNA sola vez. Antes se recargaba 2-3
        # veces (Excel + Dashboard + render). single_run las libera al
        # terminar; el costo aqui es leer 1 hoja del Excel (~10k filas).
        try:
            X_full, business_full = _load_variety_inputs(variety)
        except Exception:
            logger.exception(
                f"[{variety}] no se pudo recargar data para outputs ejecutivos"
            )
            X_full = None
            business_full = None

        # ---- Excel del CAMPEON aplicado a la data real ----
        # Una sola Excel por variedad, en reports/ junto al HTML.
        winner_excel_path = _export_winner_excel(
            champion=champion, variety=variety, logger=logger,
            X_raw=X_full, business_cols=business_full,
        )

        # ---- Dashboard ejecutivo unico (Winner_{variety}.html) ----
        if X_full is not None:
            try:
                winner_path = render_winner_dashboard(
                    variety=variety,
                    results=results,
                    champion=champion,
                    decision=champion_decision,
                    excel_path=winner_excel_path,
                    X_raw=X_full,
                )
                winner_report_path = str(winner_path)
                logger.info(f"[{variety}] Winner dashboard: {winner_path}")
                # Limpieza de residuales: deja solo el Winner_{variety}.html (+ xlsx)
                keep = [winner_path]
                if winner_excel_path:
                    keep.append(winner_excel_path)
                deleted = cleanup_residual_reports(variety=variety, keep=keep)
                if deleted:
                    logger.info(
                        f"[{variety}] Limpieza reports/: {len(deleted)} archivo(s) borrado(s)"
                    )
            except Exception:
                logger.exception(f"[{variety}] no se pudo construir Winner dashboard")

        _tag_champion(
            champion, results, variety, champion_decision, logger,
            winner_report_path=winner_report_path,
            winner_excel_path=winner_excel_path,
        )

        if args.register_model:
            registered_name = _register_champion(
                champion, variety, args, logger,
                winner_report_path=winner_report_path,
            )

    elapsed = time.perf_counter() - t_var
    summary = {
        "variety": variety,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "experiment": experiment_name,
        "model_types_attempted": model_types,
        "model_types_failed": failures,
        "registered_model": registered_name,
        "winner_report": winner_report_path,
        "winner_excel": winner_excel_path,
        "elapsed_seconds": round(elapsed, 2),
        "champion": champion_decision,
        "per_model": [
            {
                "model_type": r.model_type,
                "metrics": r.metrics,
                "full_metrics": r.full_metrics,
                "composite_score": r.composite_score,
                "abs_gap": r.abs_gap,
                "full_mape": r.full_mape if r.full_mape != float("inf") else None,
                "mlflow_run_id": r.mlflow_run_id,
                "pipeline_path": r.pipeline_path,
                "elapsed_seconds": r.elapsed_seconds,
            }
            for r in results
        ],
    }
    summary_path = ARTIFACTS_DIR / f"variety_summary_{variety}.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    return summary


def _load_variety_inputs(variety: str):
    """Recarga (X, business_cols) desde disco para la variedad indicada.

    Se usa al construir el dashboard ganador y el Excel del campeon, despues
    de que `single_run` libero las matrices grandes en su `del`. Es barato:
    una hoja del Excel de training (~10k filas).
    """
    X, _y = load_data(sheet=variety)
    business_cols = load_business_columns(sheet=variety)
    return X, business_cols


def _export_winner_excel(
    champion: ModelResult,
    variety: str,
    logger,
    *,
    X_raw,
    business_cols,
) -> Optional[str]:
    """Genera el Excel multi-hoja del CAMPEON aplicado a la data real.

    Recibe (X_raw, business_cols) ya cargados por el caller (train_variety)
    para evitar releer el Excel multiples veces. El archivo se escribe en
    `reports/` con nombre deterministico (`Winner_{variety}.xlsx`) para que
    el HTML pueda enlazarlo via path relativo.

    Devuelve la ruta como string, o None si los inputs son None / faltan
    columnas KG/JR / H-EF o si la prediccion fallo.
    """
    import joblib

    if X_raw is None or business_cols is None:
        return None

    bv = champion.business_validation
    oof = {
        "y_true": champion.oof_y_true,
        "y_pred": champion.oof_y_pred,
    }
    if oof["y_true"] is None or oof["y_pred"] is None or bv is None:
        logger.warning(
            f"[{variety}] sin datos OOF/business para Excel del campeon"
        )
        return None

    try:
        final_pipeline = joblib.load(champion.pipeline_path)
    except Exception:
        logger.exception(f"[{variety}] no se pudo cargar pipeline del campeon")
        return None

    excel_path = export_business_excel(
        variety=variety,
        model_type=champion.model_type,
        X_raw=X_raw,
        business_cols=business_cols,
        oof=oof,
        final_pipeline=final_pipeline,
        business_validation=bv,
        nested_metrics=champion.metrics,
        output_dir=REPORTS_DIR,
        filename=f"Winner_{variety}.xlsx",
    )
    if excel_path is None:
        logger.warning(
            f"[{variety}] Excel del campeon NO generado "
            "(faltan columnas KG/JR/H-EF en el dataset)"
        )
        return None
    logger.info(f"[{variety}] Excel del campeon: {excel_path}")
    return str(excel_path)


def _tag_champion(
    champion: ModelResult,
    results: list[ModelResult],
    variety: str,
    champion_decision: dict,
    logger,
    winner_report_path: Optional[str] = None,
    winner_excel_path: Optional[str] = None,
) -> None:
    """Tags + artifacts comparativos en MLflow.

    - Tags: `is_champion=true` en el run ganador, `false` en los perdedores.
      Los tags resumen del campeon (composite_score, gap, full_mape) viven
      solo en el run ganador.
    - Artifacts comparativos (decision JSON, dashboard HTML, Excel ejecutivo,
      summary multi-modelo): se loguean en TODOS los runs de la variedad
      para que cada run histórico tenga su contexto comparativo accesible
      en MLflow UI. Sin esto, abrir lgb_v20 en MLflow no muestra contra qué
      compitió ni por qué perdió.
    """
    try:
        client = mlflow.tracking.MlflowClient()

        client.set_tag(champion.mlflow_run_id, "is_champion", "true")
        client.set_tag(
            champion.mlflow_run_id,
            "champion_composite_score",
            f"{champion.composite_score:.6f}",
        )
        client.set_tag(
            champion.mlflow_run_id, "champion_abs_gap",
            f"{champion.abs_gap:.6f}",
        )
        client.set_tag(
            champion.mlflow_run_id, "champion_full_mape",
            f"{champion.full_mape:.4f}",
        )
        for r in results:
            if r.mlflow_run_id != champion.mlflow_run_id:
                client.set_tag(r.mlflow_run_id, "is_champion", "false")

        decision_path = ARTIFACTS_DIR / f"champion_{variety}.json"
        decision_path.write_text(
            json.dumps(champion_decision, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        summary_path = ARTIFACTS_DIR / f"variety_summary_{variety}.json"

        # Lista (path, mlflow_subpath) de artifacts comparativos. Se filtran
        # los None (artifacts opcionales que no se generaron) y los
        # inexistentes en disco. Loop unico evita ifs paralelos por artefacto.
        candidate_artifacts: list[tuple[Optional[str], str]] = [
            (str(decision_path), "champion"),
            (winner_report_path, "winner_dashboard"),
            (winner_excel_path, "winner_dashboard"),
            (str(summary_path) if summary_path.exists() else None, "champion"),
        ]
        artifacts = [(p, sub) for p, sub in candidate_artifacts if p]

        for r in results:
            for path, sub in artifacts:
                client.log_artifact(r.mlflow_run_id, path, artifact_path=sub)
    except Exception:
        logger.exception(f"[{variety}] no se pudo taggear campeon en MLflow")


def _register_champion(
    champion: ModelResult,
    variety: str,
    args: argparse.Namespace,
    logger,
    winner_report_path: Optional[str] = None,
) -> Optional[str]:
    """Registra el modelo campeon en MLflow Model Registry."""
    logger.info(
        f"[{variety}] Registrando CAMPEON en Model Registry: "
        f"{champion.model_type} (run={champion.mlflow_run_id[:8]}...)"
    )
    if winner_report_path:
        html_filename = winner_report_path.replace("\\", "/").rsplit("/", 1)[-1]
        report_uri = (
            f"runs:/{champion.mlflow_run_id}/winner_dashboard/{html_filename}"
        )
    else:
        report_uri = None
    # register_model devuelve None solo cuando el backend es file:// (caso
    # esperado en local). Si el backend es http:// y algo falla, levanta
    # MlflowException; lo capturamos aqui para no abortar la variedad
    # completa por un fallo del Registry (el modelo ya esta entrenado y
    # logueado).
    try:
        registered_name = register_model(
            run_id=champion.mlflow_run_id,
            artifact_name="model_pipeline",
            variety=variety,
            stage=args.registry_stage if args.registry_stage != "None" else None,
            metrics=champion.metrics,
            report_artifact_uri=report_uri,
            extra_tags={
                "tuning": args.tuning,
                "model_type": champion.model_type,
                "is_champion": "true",
                "composite_score": f"{champion.composite_score:.6f}",
            },
        )
    except Exception:
        logger.exception(
            f"[{variety}] register_model FALLO en backend remoto "
            "(auth/red/schema). El run sigue logueado pero NO se registro version."
        )
        return None
    if registered_name:
        logger.info(f"[{variety}] Registrado: {registered_name}")
    else:
        logger.warning(
            f"[{variety}] Registry no disponible (backend file://); "
            "en EC2 con Postgres si funciona el versionado"
        )
    return registered_name

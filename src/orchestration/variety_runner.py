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
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import mlflow

from src.config import ARTIFACTS_DIR, CHAMPION_MAX_GAP, CHAMPION_MAX_MAPE, REPORTS_DIR
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
from src.utils.sklearn_helpers import dump_json_artifact


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
            f"MAPE_oof={champion.oof_mape:.2f}% | "
            f"MAPE_full={champion.full_mape:.2f}% | "
            f"dt={champion.elapsed_seconds:.1f}s | "
            f"composite_aux={champion.composite_score:.4f}"
        )
        logger.info(
            f"[{variety}] Decision: {champion_decision.get('justification', '')}"
        )

        # --- Quality gate: rechazar campeon si supera umbrales minimos ---
        # Gate sobre MAPE OOF (honesto: cada fila predicha por un modelo que
        # no la vio en train), NO sobre full_mape (in-sample, optimista).
        # full_mape se sigue exponiendo en el log como referencia del modelo
        # de produccion, pero la decision de promover depende del OOF.
        # Quality gate con separacion correcta de conceptos:
        #   - MAPE_oof = calidad OPERATIVA real (lo que ve el negocio).
        #     Si supera threshold -> BLOQUEA registro (modelo inutil).
        #   - gap = sintoma DIAGNOSTICO de overfitting (diferencia train-test).
        #     Si supera threshold -> WARNING pero NO bloquea registro:
        #     un arbol boosted con gap alto puede igual generalizar bien
        #     (memoriza train por diseno; lo importante es MAE_test honesto).
        mape_ok = champion.oof_mape <= CHAMPION_MAX_MAPE
        gap_ok = (champion.abs_gap * 100) <= CHAMPION_MAX_GAP

        if not mape_ok:
            logger.warning(
                f"[{variety}] CAMPEON RECHAZADO por calidad operativa | "
                f"MAPE_oof={champion.oof_mape:.2f}% supera threshold "
                f"{CHAMPION_MAX_MAPE}%. El modelo NO se registra en Model Registry "
                f"(predice mal en datos OOF -> inutilizable en produccion). "
                f"El run SI esta en MLflow Experiments (run_id={champion.mlflow_run_id[:8]}...) "
                f"con todos sus artifacts para diagnostico."
            )
            args_register = False
        elif not gap_ok:
            logger.warning(
                f"[{variety}] CAMPEON registra con WARNING de overfitting | "
                f"gap={champion.abs_gap * 100:.2f}pp supera threshold "
                f"{CHAMPION_MAX_GAP}pp pero MAPE_oof={champion.oof_mape:.2f}% "
                f"(<={CHAMPION_MAX_MAPE}%) confirma calidad operativa OK. "
                f"El gap es diagnostico de memoria del train, NO afecta predicciones "
                f"OOF/produccion. Modelo aprobado para Registry."
            )
            args_register = args.register_model
        else:
            logger.info(
                f"[{variety}] CAMPEON pasa quality gate | "
                f"MAPE_oof={champion.oof_mape:.2f}% (max={CHAMPION_MAX_MAPE}%) | "
                f"gap={champion.abs_gap * 100:.2f}pp (max={CHAMPION_MAX_GAP}pp). "
                f"Registra en MLflow Model Registry."
            )
            args_register = args.register_model

        # Eliminar runs de modelos NO campeon de MLflow Experiments. El usuario
        # quiere ver UN solo run por training (el ganador), no los candidatos.
        # Soft delete via API: MLflow marca lifecycle_stage='deleted' en
        # Postgres (recuperable con client.restore_run). El ModelResult sigue
        # vivo en memoria asi que el dashboard HTML/Excel comparativos NO se
        # afectan.
        # Limpiar mlflow_run_id="" en el ModelResult evita que _tag_champion
        # intente operar sobre runs que ya no existen.
        losers = [r for r in results if r is not champion]
        if losers:
            client = mlflow.tracking.MlflowClient()
            deleted_count = 0
            for loser in losers:
                if not loser.mlflow_run_id:
                    continue
                try:
                    client.delete_run(loser.mlflow_run_id)
                    loser.mlflow_run_id = ""
                    deleted_count += 1
                except Exception:
                    logger.exception(
                        f"[{variety}] error eliminando run de {loser.model_type}"
                    )
            if deleted_count > 0:
                logger.info(
                    f"[{variety}] Eliminados {deleted_count}/{len(losers)} run(s) "
                    f"no campeon de MLflow (en .trash, recuperables): "
                    f"{[lo.model_type for lo in losers]}"
                )

        # Cargar (X, y, business_cols) UNA sola vez. Antes se recargaba 2-3
        # veces (Excel + Dashboard + render). single_run las libera al
        # terminar; el costo aqui es leer 1 hoja del Excel (~10k filas).
        try:
            X_full, _, business_full = _load_variety_inputs(variety)
        except Exception:
            logger.exception(
                f"[{variety}] no se pudo recargar data para outputs ejecutivos"
            )
            X_full = None
            business_full = None

        # `run_label` con segundos para evitar colision si dos runs corren en
        # el mismo minuto (smoke tests <60s). Comparte el sufijo entre
        # Winner_<variety>_<run_label>.html y .xlsx para ligar visualmente
        # ambos artefactos del mismo training.
        run_label = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        # ---- Excel del CAMPEON aplicado a la data real ----
        winner_excel_path = _export_winner_excel(
            champion=champion,
            variety=variety,
            logger=logger,
            X_raw=X_full,
            business_cols=business_full,
            run_label=run_label,
        )

        # ---- Dashboard ejecutivo por-run (Winner_{variety}_{run_label}.html) ----
        if X_full is not None:
            try:
                winner_path = render_winner_dashboard(
                    variety=variety,
                    results=results,
                    champion=champion,
                    decision=champion_decision,
                    excel_path=winner_excel_path,
                    X_raw=X_full,
                    run_label=run_label,
                )
                winner_report_path = str(winner_path)
                logger.info(f"[{variety}] Winner dashboard: {winner_path}")
                # Limpia obsoletos (reporte_*, business_export_*) pero mantiene
                # Winners por-run y residuals: el index global los lista todos.
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
            champion,
            results,
            variety,
            champion_decision,
            logger,
            winner_report_path=winner_report_path,
            winner_excel_path=winner_excel_path,
        )

        if args_register:
            registered_name = _register_champion(
                champion,
                variety,
                args,
                logger,
                winner_report_path=winner_report_path,
            )

        # Regenera el dashboard global (`reports/index.html`) DESPUES del
        # register MLflow para que el sidebar liste el Winner recien
        # generado y la pill 'Latest' apunte a el. En modo paralelo (varias
        # variedades simultaneas) hay race condition al reescribir el mismo
        # archivo: aceptable porque ambos procesos escanean reports/ y el
        # ultimo en escribir ve todos los archivos. Try/except para no
        # abortar la variedad si write_dashboard falla.
        try:
            from src.diagnostics.dashboard_index import write_dashboard
            write_dashboard(REPORTS_DIR)
        except Exception:
            logger.exception(f"[{variety}] no se pudo regenerar reports/index.html")

        # Mensaje final UNICO con champion + registry + comando UI. Sale solo
        # aqui (no por cada modelo en single_run) porque hasta ahora el champion
        # ya esta seleccionado, registrado y los losers eliminados.
        registry_line = (
            f"version registrada: {registered_name}"
            if registered_name
            else "Model Registry no aplico (backend no SQL o gate falla)"
        )
        logger.info("=" * 78)
        logger.info(
            f"[{variety}] CHAMPION FINAL: {champion.model_type} "
            f"(run={champion.mlflow_run_id[:12]}) | "
            f"MAE_test={champion.metrics.get('nested_cv_mae_mean', 0):.4f} | "
            f"|gap|={champion.abs_gap:.4f} | "
            f"MAPE_oof={champion.oof_mape:.2f}%"
        )
        logger.info(f"[{variety}] {registry_line}")
        logger.info(
            f"[{variety}] MLflow UI: http://localhost:5000 "
            "(local: `task up`; AWS: ALB del modulo mlflow)"
        )
        logger.info("=" * 78)

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
    dump_json_artifact(
        ARTIFACTS_DIR / f"variety_summary_{variety}.json",
        summary,
    )
    return summary


def _load_variety_inputs(variety: str):
    """Recarga (X, y, business_cols) desde disco para la variedad indicada.

    Se usa al construir el dashboard ganador, el Excel del campeon, y el
    permutation_importance del feature importance, despues de que
    `single_run` libero las matrices grandes en su `del`. Es barato:
    una hoja del Excel de training (~10k filas).
    """
    X, y = load_data(sheet=variety)
    business_cols = load_business_columns(sheet=variety)
    return X, y, business_cols


def _export_winner_excel(
    champion: ModelResult,
    variety: str,
    logger,
    *,
    X_raw,
    business_cols,
    run_label: Optional[str] = None,
) -> Optional[str]:
    """Genera el Excel multi-hoja del CAMPEON aplicado a la data real.

    Recibe (X_raw, business_cols) ya cargados por el caller (train_variety)
    para evitar releer el Excel multiples veces. El archivo se escribe en
    `reports/` con nombre `Winner_{variety}_{run_label}.xlsx` (acumula uno
    por run); si `run_label` es None cae al patron viejo
    `Winner_{variety}.xlsx` (sobrescribe).

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
        logger.warning(f"[{variety}] sin datos OOF/business para Excel del campeon")
        return None

    try:
        final_pipeline = joblib.load(champion.pipeline_path)
    except Exception:
        logger.exception(f"[{variety}] no se pudo cargar pipeline del campeon")
        return None

    excel_filename = (
        f"Winner_{variety}_{run_label}.xlsx" if run_label
        else f"Winner_{variety}.xlsx"
    )
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
        filename=excel_filename,
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
            champion.mlflow_run_id,
            "champion_abs_gap",
            f"{champion.abs_gap:.6f}",
        )
        client.set_tag(
            champion.mlflow_run_id,
            "champion_full_mape",
            f"{champion.full_mape:.4f}",
        )
        client.set_tag(
            champion.mlflow_run_id,
            "champion_oof_mape",
            f"{champion.oof_mape:.4f}",
        )

        # Tags de drift/EDA: si existe un EDA sidecar reciente, capturamos el
        # peor PSI y count de severidad para que MLflow UI muestre cuando el
        # data drift se vuelve critico run-tras-run sin abrir HTMLs manualmente.
        try:
            from src.diagnostics.eda import (
                extract_drift_summary,
                find_latest_eda_sidecar,
            )
            sidecar = find_latest_eda_sidecar(variety)
            if sidecar is not None:
                for k, v in extract_drift_summary(sidecar).items():
                    if v:  # skip empty strings
                        client.set_tag(champion.mlflow_run_id, k, v)
                logger.info(f"[{variety}] Drift tags MLflow desde {sidecar.name}")
        except Exception:
            logger.exception(f"[{variety}] no se pudo taggear drift (no critico)")

        for r in results:
            # r.mlflow_run_id puede estar vacio si el run fue eliminado por
            # ser loser (cleanup post-quality-gate). En ese caso skip:
            # ya no existe en MLflow para taggearlo.
            if not r.mlflow_run_id:
                continue
            if r.mlflow_run_id != champion.mlflow_run_id:
                client.set_tag(r.mlflow_run_id, "is_champion", "false")

        decision_path = dump_json_artifact(
            ARTIFACTS_DIR / f"champion_{variety}.json",
            champion_decision,
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
            # Skip losers ya eliminados (mlflow_run_id="" tras cleanup)
            if not r.mlflow_run_id:
                continue
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
        report_uri = (
            f"runs:/{champion.mlflow_run_id}/winner_dashboard/"
            f"{Path(winner_report_path).name}"
        )
    else:
        report_uri = None
    # register_model devuelve None cuando el backend es file:// (caso default
    # del proyecto local). Si MLFLOW_TRACKING_URI apunta a un backend SQL y
    # algo falla, MlflowException PROPAGA; la capturamos aqui para no
    # abortar la variedad por un fallo del Registry (el modelo ya esta
    # entrenado y logueado en el run).
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
            f"[{variety}] register_model FALLO (auth/red/schema). "
            "El run sigue logueado pero NO se registro version."
        )
        return None
    if registered_name:
        logger.info(f"[{variety}] Registrado: {registered_name}")
    else:
        logger.warning(
            f"[{variety}] Registry no disponible (backend file://); "
            "para versionado configura MLFLOW_TRACKING_URI con un SQL backend."
        )
    return registered_name

"""Entrypoint del pipeline ML.

main.py es THIN: parse args -> bootstrap -> delega en `src/orchestration/`.

Uso normal (via Taskfile):
    task train VARIETIES=POP
    task train VARIETIES=POP,VENTURA
    task train VARIETIES=all

Uso directo (CLI avanzado):
    python main.py --varieties POP
    python main.py --varieties POP,JUPITER
    python main.py --varieties all --tuning prod_xl
    python main.py --varieties POP --model xgb   # fuerza un solo backend

Defaults: --tuning prod | --model auto (entrena XGB y LGB, elige campeon por
variedad). Cada variedad es independiente: si una falla, las demas continuan.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime

from src.config import (
    ARTIFACTS_DIR,
    MLFLOW_TRACKING_URI,
    REPORTS_DIR,
    S3_ARTIFACTS_BUCKET,
    S3_ARTIFACTS_PREFIX,
    S3_REPORTS_PREFIX,
    TRAINING_FILE,
    init_dirs,
)
from src.orchestration.cli import (
    parse_args,
    resolve_models,
    resolve_settings,
    resolve_varieties,
)
from src.orchestration.runners import run_parallel, run_sequential
from src.step_06_track.mlflow_registry import init_mlflow
from src.utils.logger import setup_logging
from src.utils.sklearn_helpers import dump_json_artifact


def _resolve_parallelism(args, settings: dict) -> int:
    """Ajusta `inner_cv_n_jobs` cuando hay paralelismo de variedades para
    evitar oversubscription de CPU."""
    parallel = max(1, int(args.parallel_varieties))
    inner_n_jobs = args.inner_cv_n_jobs
    if parallel > 1 and inner_n_jobs == -1:
        cores = os.cpu_count() or 4
        inner_n_jobs = max(1, cores // parallel)
    settings["inner_cv_n_jobs"] = inner_n_jobs
    return parallel


def _write_aggregate_summary(
    aggregate: dict[str, dict],
    failed_varieties: list[str],
    varieties: list[str],
    models: list[str],
    elapsed_seconds: float,
) -> "ARTIFACTS_DIR.__class__":  # path-like
    return dump_json_artifact(
        ARTIFACTS_DIR / "run_summary_AGGREGATE.json",
        {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "elapsed_seconds_total": round(elapsed_seconds, 2),
            "n_varieties": len(varieties),
            "n_failed_varieties": len(failed_varieties),
            "failed_varieties": failed_varieties,
            "models_trained": models,
            "champions": {
                v: data.get("champion", {}).get("champion_model")
                for v, data in aggregate.items()
                if isinstance(data, dict) and data.get("champion") is not None
            },
            "per_variety": aggregate,
        },
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = resolve_settings(args)
    init_dirs()  # crea logs/, artifacts/, reports/, mlruns/ (idempotente)
    logger = setup_logging()

    if not TRAINING_FILE.exists():
        logger.error(
            f"No existe el archivo de training: {TRAINING_FILE}. "
            f"Corre `task data:split` (o data:prepare en EC2) primero."
        )
        return 2

    try:
        varieties = resolve_varieties(args.varieties)
        models = resolve_models(args.model)
    except ValueError as exc:
        logger.error(str(exc))
        return 2
    if not varieties or not models:
        logger.error("varieties/models vacios; revisa --varieties y --model")
        return 2

    parallel = _resolve_parallelism(args, settings)

    logger.info("=" * 78)
    logger.info(
        f"Inicio | tuning={args.tuning} | models={models} | varieties={varieties}"
    )
    logger.info(
        f"Paralelismo: variedades={parallel} | "
        f"inner_cv_n_jobs={settings['inner_cv_n_jobs']} | cores={os.cpu_count()}"
    )
    logger.info(f"MLflow tracking URI: {MLFLOW_TRACKING_URI}")
    logger.info("=" * 78)

    init_mlflow()  # solo seteamos URI; experimento se setea por variedad

    t_total = time.perf_counter()

    if parallel > 1 and len(varieties) > 1:
        aggregate, failed_varieties = run_parallel(
            varieties,
            models,
            args,
            settings,
            logger,
            n_workers=min(parallel, len(varieties)),
        )
    else:
        aggregate, failed_varieties = run_sequential(
            varieties,
            models,
            args,
            settings,
            logger,
        )

    total_dt = time.perf_counter() - t_total
    aggregate_path = _write_aggregate_summary(
        aggregate,
        failed_varieties,
        varieties,
        models,
        total_dt,
    )

    logger.info("=" * 78)
    logger.info(
        f"FIN | variedades={len(varieties)} | falladas={len(failed_varieties)} "
        f"| tiempo_total={total_dt:.1f}s"
    )
    logger.info("Campeones por variedad:")
    for v, data in aggregate.items():
        if isinstance(data, dict) and data.get("champion"):
            ch = data["champion"]
            logger.info(
                f"  {v:25s} -> {ch['champion_model']:5s} "
                f"composite={ch['champion_composite_score']:.4f}"
            )
    logger.info(f"Resumen agregado: {aggregate_path}")
    logger.info("=" * 78)

    # S3 sync: solo si S3_ARTIFACTS_BUCKET esta configurado (EC2/CI).
    # En local el bucket esta vacio -> se omite silenciosamente.
    if S3_ARTIFACTS_BUCKET:
        from scripts.s3_sync import sync_to_s3

        sync_to_s3(
            artifacts_dir=ARTIFACTS_DIR,
            reports_dir=REPORTS_DIR,
            bucket=S3_ARTIFACTS_BUCKET,
            artifacts_prefix=S3_ARTIFACTS_PREFIX,
            reports_prefix=S3_REPORTS_PREFIX,
        )

    return 0 if not failed_varieties else 1


if __name__ == "__main__":
    sys.exit(main())

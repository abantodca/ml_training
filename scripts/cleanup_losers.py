"""Limpieza retroactiva de runs no-campeon en MLflow Experiments.

Recorre experimentos (uno o todos) y elimina runs con tag
`is_champion=false`, dejando solo los campeones visibles. Por defecto
corre en modo DRY-RUN: reporta sin tocar nada. Usar `--apply` para ejecutar.

Los runs se marcan como `deleted` via la API (soft delete en Postgres).
Recuperables con `client.restore_run(run_id)`.

Uso
---
    # ver que se eliminaria (dry-run, default)
    python -m scripts.cleanup_losers

    # eliminar (todos los experimentos)
    python -m scripts.cleanup_losers --apply

    # solo un experimento (variedad)
    python -m scripts.cleanup_losers --apply --variety POP

    # tambien runs SIN tag is_champion (crashed mid-training, runs
    # legacy pre-cambio, etc.) -- usar con cuidado
    python -m scripts.cleanup_losers --apply --aggressive

Que SI se elimina (modo default)
--------------------------------
- Runs con tag `is_champion=false`: confirmados como modelos perdedores
  por el lex-order del champion selector.

Que NO se elimina (modo default)
--------------------------------
- Runs con tag `is_champion=true`: campeones, se preservan.
- Runs sin tag `is_champion`: legacy / crashed. Salvo --aggressive,
  los respeta para no perder informacion accidentalmente.
- Runs ya eliminados (lifecycle_stage=deleted): MLflow los excluye
  por default (search_runs usa ACTIVE_ONLY).
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Tuple

import mlflow
from mlflow.tracking import MlflowClient

from src.config import MLFLOW_TRACKING_URI


def find_loser_runs(
    client: MlflowClient,
    experiment_id: str,
    aggressive: bool = False,
) -> List[Tuple[str, str, str, str]]:
    """Devuelve [(run_id, run_name, model_type, reason)] de candidatos."""
    runs = client.search_runs([experiment_id], max_results=10000)
    candidates: List[Tuple[str, str, str, str]] = []
    for run in runs:
        is_champion = run.data.tags.get("is_champion")
        run_name = (
            run.data.tags.get("mlflow.runName")
            or getattr(run.info, "run_name", "")
            or ""
        )
        model_type = run.data.tags.get("model_type", "?")
        if is_champion == "false":
            candidates.append(
                (run.info.run_id, run_name, model_type, "tagged_loser")
            )
        elif aggressive and is_champion is None:
            candidates.append(
                (run.info.run_id, run_name, model_type, "no_champion_tag")
            )
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Elimina runs no-campeon de MLflow Experiments."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Ejecutar la eliminacion. Sin esto, solo dry-run.",
    )
    parser.add_argument(
        "--variety",
        default=None,
        help="Limita a UN experimento (e.g. POP). Sin esto, todos.",
    )
    parser.add_argument(
        "--aggressive",
        action="store_true",
        help="Tambien elimina runs sin tag is_champion (crashed/legacy).",
    )
    args = parser.parse_args()

    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    client = MlflowClient()

    if args.variety:
        exp = client.get_experiment_by_name(args.variety)
        if exp is None:
            print(f"ERROR: experiment '{args.variety}' no encontrado.")
            sys.exit(1)
        experiments = [exp]
    else:
        experiments = client.search_experiments()

    print(f"Modo:         {'APPLY (eliminacion real)' if args.apply else 'DRY-RUN (sin tocar nada)'}")
    print(f"Tracking URI: {MLFLOW_TRACKING_URI}")
    print(f"Aggressive:   {args.aggressive}")
    print(f"Scope:        {'1 experimento (' + args.variety + ')' if args.variety else 'todos los experimentos'}")
    print()

    total_candidates = 0
    total_deleted = 0
    total_errors = 0
    total_experiments_with_candidates = 0

    for exp in experiments:
        candidates = find_loser_runs(client, exp.experiment_id, args.aggressive)
        if not candidates:
            continue

        total_experiments_with_candidates += 1
        print(f"=== Experiment: {exp.name} (id={exp.experiment_id}) ===")
        print(f"Candidatos: {len(candidates)}")
        for run_id, run_name, model_type, reason in candidates:
            total_candidates += 1
            short_id = run_id[:12]
            tag_padded = f"{run_name:25s}"[:25]
            print(f"  - {tag_padded}  {model_type:5s}  [{reason:16s}]  {short_id}...")
            if args.apply:
                try:
                    client.delete_run(run_id)
                    total_deleted += 1
                except Exception as exc:
                    total_errors += 1
                    print(f"    ERROR al eliminar: {exc}")
        print()

    print("=" * 70)
    print(f"Experimentos afectados: {total_experiments_with_candidates}")
    print(f"Runs encontrados:       {total_candidates}")
    if args.apply:
        print(f"Runs eliminados:        {total_deleted}")
        if total_errors:
            print(f"Errores:                {total_errors}")
        if total_deleted > 0:
            print()
            print(f"Runs marcados como `deleted` en Postgres (soft delete).")
            print(f"Para recuperar: client.restore_run(run_id) en Python.")
            print(f"Para purgar definitivamente: `mlflow gc` en CLI.")
    else:
        print(f"Eliminacion: NO ejecutada (modo dry-run).")
        if total_candidates > 0:
            print(f"Para ejecutar: agrega --apply a este comando.")


if __name__ == "__main__":
    main()

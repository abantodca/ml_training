"""Auditor del MLflow Model Registry — read-only.

Lista todas las versions de cada modelo registrado, con:
    - version + stage actual
    - run_id origen
    - composite_score / MAPE_oof / gap (de los tags del run)
    - is_champion del run

Recomienda que versions archivar (mantener solo el campeon vigente),
pero NO ejecuta cambios. La transicion la hace el usuario via UI
(http://localhost:5000 → Models) o con `mlflow.MlflowClient`:

    client.transition_model_version_stage(
        name="rnd-forest-POP", version="2", stage="Archived"
    )

Por que no auto-archivar: las decisiones de Registry son operacionales
(¿cual es Production hoy? ¿cual es Staging?), no pertenecen al training.
Mantenerlas manuales evita que un re-train accidental degrade el modelo
de produccion.

Uso:
    docker compose run --rm --no-deps --entrypoint python trainer \\
        -m scripts.registry_audit
"""
from __future__ import annotations

import argparse
from typing import Optional

import mlflow
from mlflow.tracking import MlflowClient

from src.config import MLFLOW_TRACKING_URI


def _safe_metric(run, key: str) -> Optional[float]:
    """Lee una metrica del run, devuelve None si falta."""
    return run.data.metrics.get(key)


def _safe_tag(run, key: str) -> str:
    return run.data.tags.get(key, "")


def audit_model(client: MlflowClient, model_name: str) -> None:
    """Imprime tabla con versions del modelo + metricas + recomendacion."""
    try:
        versions = client.search_model_versions(f"name='{model_name}'")
    except Exception as e:
        print(f"  [ERROR] no se pudo listar versions de {model_name}: {e}")
        return

    if not versions:
        print(f"  (sin versions registradas)")
        return

    rows = []
    for v in versions:
        try:
            run = client.get_run(v.run_id)
        except Exception:
            run = None

        composite = _safe_metric(run, "nested_cv_mae_mean") if run else None
        mape_oof = _safe_metric(run, "business_mape_oof") if run else None
        gap = _safe_metric(run, "nested_cv_gap_mean") if run else None
        is_champion = _safe_tag(run, "is_champion") if run else "?"
        model_type = _safe_tag(run, "model_type") if run else "?"
        git_commit = _safe_tag(run, "git_commit") if run else "?"

        rows.append({
            "version": v.version,
            "stage": v.current_stage,
            "run_id": v.run_id[:12],
            "model_type": model_type,
            "mape_oof": mape_oof,
            "gap": gap,
            "is_champion": is_champion,
            "git_commit": git_commit,
        })

    print(f"  {'v':>3s}  {'stage':10s}  {'run_id':14s}  {'model':5s}  "
          f"{'MAPE_oof':>9s}  {'gap':>7s}  {'champion':9s}  {'commit':10s}")
    print(f"  {'-' * 3:>3s}  {'-' * 10:10s}  {'-' * 14:14s}  {'-' * 5:5s}  "
          f"{'-' * 9:>9s}  {'-' * 7:>7s}  {'-' * 9:9s}  {'-' * 10:10s}")
    rows.sort(key=lambda r: int(r["version"]))
    for r in rows:
        mape = f"{r['mape_oof']:.4f}" if r["mape_oof"] is not None else "—"
        gap = f"{r['gap']:+.4f}" if r["gap"] is not None else "—"
        print(f"  {r['version']:>3s}  {r['stage'] or 'None':10s}  "
              f"{r['run_id']:14s}  {r['model_type']:5s}  "
              f"{mape:>9s}  {gap:>7s}  {r['is_champion']:9s}  "
              f"{r['git_commit'][:10]:10s}")

    # Recomendacion: el mejor MAPE_oof = candidate to Production
    valid = [r for r in rows if r["mape_oof"] is not None]
    if valid:
        best = min(valid, key=lambda r: r["mape_oof"])
        print()
        print(f"  Recomendacion: v{best['version']} (MAPE_oof={best['mape_oof']:.4f}) "
              f"deberia ser Production.")
        loser_versions = [r["version"] for r in valid if r["version"] != best["version"]]
        if loser_versions:
            print(f"  Archivar: v{', v'.join(loser_versions)}")
            print(f"  Comando para archivar (ejemplo):")
            print(f"    client.transition_model_version_stage(")
            print(f"        name='{model_name}', version='{loser_versions[0]}', stage='Archived')")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audita MLflow Model Registry")
    parser.add_argument("--model", default=None,
                        help="Nombre del modelo (default: todos los registrados)")
    args = parser.parse_args()

    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    client = MlflowClient()

    if args.model:
        models = [args.model]
    else:
        try:
            models = [m.name for m in client.search_registered_models()]
        except Exception as e:
            print(f"[ERROR] no se pudo listar modelos: {e}")
            return 1

    if not models:
        print("Registry vacio. Corre `task train` primero.")
        return 0

    for name in models:
        print(f"\nModel: {name}")
        audit_model(client, name)

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

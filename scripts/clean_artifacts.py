"""Limpia artifacts/ conservando los ultimos N runs por (variety, model).

Cada llamada a `single_run.train_model` persiste 3 archivos versionados por
`run_name` (xgb_v20, lgb_v3, ...):
    artifacts/final_pipeline_<variety>_<model>_v<N>.joblib
    artifacts/run_summary_<variety>_<model>_v<N>.json
    artifacts/best_params_<variety>_<model>_v<N>.json

MLflow conserva el historial completo de runs (cada uno con su `run_id` y
artifacts en `mlruns/`), asi que la limpieza local NO pierde informacion:
solo libera disco. Para recuperar un run viejo borrado de aqui, descargar
desde MLflow UI o via `mlflow.artifacts.download_artifacts`.

Uso:
    python -m scripts.clean_artifacts --keep 10
    python -m scripts.clean_artifacts --keep 10 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

from src.config import ARTIFACTS_DIR

logger = logging.getLogger(__name__)

# (prefix, ext) — todos los archivos versionados por run_name
PATTERNS: List[Tuple[str, str]] = [
    ("final_pipeline", "joblib"),
    ("run_summary", "json"),
    ("best_params", "json"),
]

# Captura `<variety>_<model>_v<N>` desde el stem (sin prefix). El model_type
# se ancla a xgb|lgb (los unicos backends soportados hoy) para que variety
# pueda contener guiones bajos sin romper el parseo.
_NAME_RE = re.compile(r"^(?P<variety>.+)_(?P<model>xgb|lgb)_v(?P<version>\d+)$")


def _scan(
    artifacts_dir: Path,
) -> Dict[Tuple[str, str, str, str], List[Tuple[int, Path]]]:
    """Indexa archivos por (prefix, ext, variety, model) -> [(version, path), ...]."""
    groups: Dict[Tuple[str, str, str, str], List[Tuple[int, Path]]] = defaultdict(list)
    for prefix, ext in PATTERNS:
        for path in artifacts_dir.glob(f"{prefix}_*_v*.{ext}"):
            tail = path.stem[len(prefix) + 1:]
            m = _NAME_RE.match(tail)
            if not m:
                continue
            groups[(prefix, ext, m.group("variety"), m.group("model"))].append(
                (int(m.group("version")), path)
            )
    return groups


def clean(artifacts_dir: Path, keep: int, dry_run: bool = False) -> int:
    """Borra archivos viejos manteniendo los `keep` mas recientes por grupo.

    Devuelve la cantidad de archivos borrados (o que se borrarian en dry_run).
    """
    if not artifacts_dir.is_dir():
        logger.info(f"No existe {artifacts_dir}; nada que limpiar.")
        return 0

    groups = _scan(artifacts_dir)
    total = 0
    for (prefix, ext, variety, model), versions in sorted(groups.items()):
        versions.sort(reverse=True)  # version mas alta primero
        to_delete = versions[keep:]
        if not to_delete:
            continue
        logger.info(
            f"[{prefix}_{variety}_{model}.{ext}] {len(versions)} archivos -> "
            f"conservar {keep}, borrar {len(to_delete)}"
        )
        for _v, path in to_delete:
            if dry_run:
                logger.info(f"  DRY-RUN borraria: {path.name}")
                total += 1
            else:
                path.unlink(missing_ok=True)
                total += 1
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", type=int, default=10,
        help="Versiones a conservar por (variety, model). Default 10.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Lista que se borraria sin borrar.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    n = clean(ARTIFACTS_DIR, keep=args.keep, dry_run=args.dry_run)
    label = "borrarian (dry-run)" if args.dry_run else "borrados"
    logger.info(f"DONE | archivos {label}: {n} | KEEP={args.keep}")


if __name__ == "__main__":
    main()

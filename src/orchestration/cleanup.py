"""Cleanup defensivo entre runs: figuras matplotlib, MLflow runs huerfanos, GC.

Tambien centraliza la limpieza de archivos residuales en `reports/` que
sobreviven a re-corridas de la misma variedad (HTMLs/Excels viejos sin el
campeon actual).
"""
from __future__ import annotations

import gc
from pathlib import Path
from typing import Iterable, List, Optional

import matplotlib.pyplot as plt
import mlflow

from src.config import REPORTS_DIR

# matplotlib backend ya esta forzado a 'Agg' en src/__init__.py (side-effect
# del paquete raiz). Importar pyplot aqui es seguro porque cualquier
# `import src.*` carga primero el __init__ del paquete.


def cleanup_state(logger, label: str) -> None:
    """Cierra figuras, termina runs MLflow huerfanos y forza GC.

    Llamar entre modelos y entre variedades para evitar:
      - Acumulacion de figuras matplotlib en memoria (RAM creciente).
      - Runs MLflow abiertos por errores no capturados (corrompen tags).
      - Memoria sin liberar de XGB/LGB que mantienen pools internos.
    """
    plt.close("all")
    if mlflow.active_run() is not None:
        mlflow.end_run()
    collected = gc.collect()
    logger.info(f"Cache limpiado ({label}) | gc liberados={collected}")


def cleanup_residual_reports(
    *, variety: str, keep: Iterable[Path | str], reports_dir: Optional[Path] = None,
) -> List[Path]:
    """Borra HTML/Excel residuales de la variedad que no esten en `keep`.

    Cada re-corrida genera `Winner_{variety}.html` + `.xlsx`. Esta funcion
    barre `reports/` y elimina cualquier otro artefacto vinculado a la
    variedad que no este en la lista `keep`. Asi reports/ queda limpio
    con un solo par de archivos por variedad.
    """
    rdir = Path(reports_dir) if reports_dir else REPORTS_DIR
    keep_set = {Path(p).resolve() for p in keep}
    deleted: List[Path] = []
    patterns = [
        f"reporte_{variety}_*.html",
        f"Winner_{variety}*.html",
        f"business_export_{variety}_*.xlsx",
        f"Winner_{variety}*.xlsx",
    ]
    for pat in patterns:
        for f in rdir.glob(pat):
            if f.resolve() in keep_set:
                continue
            try:
                f.unlink()
                deleted.append(f)
            except OSError:
                pass
    return deleted

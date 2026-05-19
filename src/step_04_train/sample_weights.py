"""Sample weights basados en la distribucion del target.

Compensa el sesgo "regresion a la media" de los arboles dando mas peso a
regiones de target raras (cola alta/baja) sin amplificar outliers.

Esta logica vivia inline en `tuning.py`, pero es generica del target y
reutilizable. Extraerla aqui permite:
- Probar otras estrategias (qcut, kde-based) sin tocar el nested CV.
- Testear unitariamente el balanceo de pesos.
- Llamarla desde scripts/notebooks de exploracion.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute_sample_weights(
    y: pd.Series,
    n_bins: int = 20,
    weight_cap: float = 5.0,
) -> np.ndarray:
    """Pesos inversos a la densidad del target con bins de IGUAL ANCHO.

    `qcut` (igual frecuencia) daria pesos uniformes; `cut` (igual ancho)
    hace que las colas (target muy alto/bajo, poca masa) reciban mas peso.

    Pipeline:
      1. Bins de igual ancho sobre el rango del target.
      2. Peso por fila = 1 / count(bin_de_la_fila).
      3. Cap a `weight_cap * mean` para evitar que 1-2 outliers extremos
         dominen el loss.
      4. `sqrt` para saturar la cola larga (con cap=5 y bins iguales el
         max post-norm subia a ~8x; sqrt comprime el rango dinamico
         conservando el ranking entre bins).
      5. Normalizar a media=1 para que el peso total no altere la escala
         de la loss (sigue siendo MAE en KG/JR_H).
    """
    y_arr = np.asarray(y, dtype=float)
    n = len(y_arr)

    # `pd.cut` puede devolver NaN cuando y contiene NaN o en casos borde
    # (ej: valores fuera del rango de bins por float-precision). Mapear NaN
    # a un bin sentinel -1 antes de indexar evita ValueError al hacer
    # int(NaN). Las filas con bin=-1 reciben weight neutral (1.0) y por
    # tanto no contribuyen al re-balanceo.
    bins = pd.cut(y_arr, bins=n_bins, labels=False, include_lowest=True)
    bins = pd.Series(bins).fillna(-1).astype(int).to_numpy()
    counts = pd.Series(bins).value_counts().to_dict()

    weights = np.array(
        [1.0 if b == -1 else 1.0 / max(counts.get(int(b), 1), 1) for b in bins],
        dtype=float,
    )
    weights = np.minimum(weights, weight_cap * weights.mean())
    weights = np.sqrt(weights)
    weights = weights * (n / weights.sum())
    return weights

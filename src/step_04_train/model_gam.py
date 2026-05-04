"""Factory de LinearGAM (pyGAM) para usar como META-learner en stacking.

El GAM aqui NO predice directamente sobre features raw del dataset: vive
en la capa de stacking encima del campeon (XGB o LGB) y recibe como
inputs:
    - col 0       : pred_base (la prediccion del modelo base)
    - col 1..K-1  : un subset acotado de features raw (`STACKING_X_SUBSET`).

Cada columna se modela con un termino del GAM:
    - `s(i)` (spline) para columnas continuas.
    - `f(i)` (factor) para columnas categoricas label-encoded. El factor
       term aprende UN intercept por nivel, sin asumir orden -- ideal
       para FORMATO o niveles binarios (flag `__ISNAN`).

Ventajas de mezclar s/f:
    1. FORMATO con n chico: el factor evita que un spline se pegue al
       ruido de pocas observaciones por nivel.
    2. Flags __ISNAN binarias: el factor expone "imputed vs observed"
       sin que el spline alise ese paso 0->1 como si fuera continuo.
    3. Interpretabilidad: el GAM da un intercept claro por nivel,
       graficable directo en el dashboard.

Anti-overfitting:
    - `monotonic_pred_base`: constraint que fuerza s(pred_base) creciente.
       Sin esto el GAM podria invertir la direccion del base en regiones
       con poca data y romper inferencia. Con la constraint el meta solo
       puede ajustar la magnitud de la prediccion, no su signo.

Devolvemos un LinearGAM "pelado" (sin link function): el target ya pasa
por log1p+cap en el pipeline del base, asi que el meta opera en KG/JR_H
linealmente. Cambiar a `GAM(distribution=...)` aqui sin tocar `stacking.py`
si en el futuro queremos modelar y con una distribucion distinta.
"""
from __future__ import annotations

from functools import reduce
from operator import add as op_add
from typing import Iterable, Optional

from pygam import LinearGAM, f, s

from src.config import STACKING_GAM_LAM, STACKING_GAM_N_SPLINES


def get_gam_meta_model(
    n_features: int,
    cat_indices: Optional[Iterable[int]] = None,
    n_splines: Optional[int] = None,
    lam: Optional[float] = None,
    spline_order: int = 3,
    monotonic_pred_base: bool = True,
) -> LinearGAM:
    """Construye un LinearGAM con un termino por feature.

    Parametros
    ----------
    n_features : numero TOTAL de columnas de entrada (incluye pred_base
                 en la columna 0). Debe ser >=1.
    cat_indices : indices de columnas categoricas (label-encoded en el
                 caller, ver `StackedRegressor`). Esos terminos usan
                 `f()` (factor) en vez de `s()` (spline). Default None
                 = todo es continuo (comportamiento legacy).
    n_splines  : nodos por spline. Default `STACKING_GAM_N_SPLINES`.
    lam        : penalizacion de smoothness (mayor = curva mas suave;
                 mejor para datasets con cola larga / shift CV->prod).
                 Default `STACKING_GAM_LAM`.
    spline_order : 3 = cubico (default). 2 = cuadratico (menos wiggle
                   en colas).
    monotonic_pred_base : si True (default), aplica constraint
                          'monotonic_inc' a `s(0)` (pred_base). Garantiza
                          que el meta no invierta la direccion del base
                          en regiones con poca data. Solo se aplica si
                          0 NO esta en cat_indices.

    Returns
    -------
    LinearGAM unfitted, listo para `gam.fit(X, y, weights=...)`.
    """
    if n_features < 1:
        raise ValueError(
            f"get_gam_meta_model: n_features debe ser >=1 (recibido {n_features})"
        )

    n_splines = STACKING_GAM_N_SPLINES if n_splines is None else int(n_splines)
    lam = STACKING_GAM_LAM if lam is None else float(lam)
    cat_set = set(cat_indices or ())

    def _term_for(i: int):
        if i in cat_set:
            # f() acepta lam pero no spline_order ni constraints (no aplica).
            return f(i, lam=lam)
        kwargs = {"n_splines": n_splines, "lam": lam, "spline_order": spline_order}
        if i == 0 and monotonic_pred_base and 0 not in cat_set:
            kwargs["constraints"] = "monotonic_inc"
        return s(i, **kwargs)

    terms = reduce(op_add, (_term_for(i) for i in range(n_features)))
    return LinearGAM(terms)

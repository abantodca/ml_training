"""Tests estadisticos para diagnostico de modelo y decisiones de promocion.

Centraliza herramientas con sustento estadistico que NO afectan el modelo:
solo lo evaluan rigurosamente. Se llaman desde el WinnerKit / dashboard
para reemplazar metricas puntuales (MAE = 0.683) con metricas + intervalos
de confianza (MAE = 0.683 [0.671, 0.695]).

Funciones:
  - bootstrap_metric_ci    : IC 95% de cualquier metrica via bootstrap percentile.
  - breusch_pagan_test     : test de heteroscedasticidad sobre residuos.
  - paired_bootstrap_diff  : IC para diferencia entre dos modelos (modelo A mejor que B?).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MetricCI:
    """Estimador puntual + IC bootstrap de una metrica."""

    point: float           # estimador sobre la muestra completa
    ci_low: float          # percentil alpha/2 de los resamples
    ci_high: float         # percentil 1-alpha/2 de los resamples
    n_resamples: int
    alpha: float

    def __str__(self) -> str:
        return f"{self.point:.4f} [{self.ci_low:.4f}, {self.ci_high:.4f}]"


def bootstrap_metric_ci(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    metric_fn: Callable[[np.ndarray, np.ndarray], float],
    n_resamples: int = 1000,
    alpha: float = 0.05,
    random_state: int = 42,
) -> MetricCI:
    """IC bootstrap (percentile method, Efron 1979).

    Resamplea (y_true, y_pred) con reemplazo `n_resamples` veces, calcula
    `metric_fn` en cada resample, y devuelve los percentiles alpha/2 y
    1-alpha/2 como banda IC.

    Para `metric_fn` use sklearn.metrics directos: `mean_absolute_error`,
    `r2_score`, etc. Para metricas custom (MAPE), pasar callable.

    Asunciones: muestras i.i.d. (cumple para OOF predictions de un modelo
    fitteado; cada fila vista por un modelo distinto).
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    n = len(y_true)
    if n == 0:
        return MetricCI(float("nan"), float("nan"), float("nan"), 0, alpha)

    rng = np.random.default_rng(random_state)
    resampled = np.empty(n_resamples, dtype=float)
    for i in range(n_resamples):
        idx = rng.integers(0, n, size=n)
        resampled[i] = metric_fn(y_true[idx], y_pred[idx])

    point = float(metric_fn(y_true, y_pred))
    ci_low = float(np.percentile(resampled, 100 * alpha / 2))
    ci_high = float(np.percentile(resampled, 100 * (1 - alpha / 2)))
    return MetricCI(point=point, ci_low=ci_low, ci_high=ci_high,
                    n_resamples=n_resamples, alpha=alpha)


@dataclass(frozen=True)
class HeteroscedasticityTest:
    """Resultado de Breusch-Pagan."""

    lm_stat: float          # estadistico de Lagrange Multiplier
    p_value: float          # p-value (H0: homoscedasticidad)
    is_heteroscedastic: bool  # True si p < 0.05
    note: str               # interpretacion en lenguaje natural


def breusch_pagan_test(
    residuals: np.ndarray,
    predictions: np.ndarray,
    alpha: float = 0.05,
) -> HeteroscedasticityTest:
    """Test de Breusch-Pagan: regresa residuos^2 contra predictions.

    H0: var(residual) constante (homoscedastico).
    H1: var(residual) varia con la magnitud predicha.

    Si p < alpha, rechazamos H0 -> intervalos simetricos del modelo
    (`mean +/- 1.96*std`) NO son validos en todos los rangos. Es la
    senal estadistica para usar Conformal Prediction (que NO asume
    homoscedasticidad).

    Implementacion: regresion OLS de residuos^2 ~ predictions, test LM.
    """
    residuals = np.asarray(residuals, dtype=float)
    predictions = np.asarray(predictions, dtype=float)
    n = len(residuals)
    if n < 10:
        return HeteroscedasticityTest(
            lm_stat=float("nan"), p_value=float("nan"),
            is_heteroscedastic=False,
            note="Muestra insuficiente para test BP (n<10).",
        )

    try:
        from statsmodels.stats.diagnostic import het_breuschpagan
        from statsmodels.regression.linear_model import OLS
        from statsmodels.tools.tools import add_constant
    except ImportError:
        return HeteroscedasticityTest(
            lm_stat=float("nan"), p_value=float("nan"),
            is_heteroscedastic=False,
            note="statsmodels no disponible para BP test.",
        )

    # OLS auxiliar: residuals ~ predictions (1 regresor)
    exog = add_constant(predictions)
    model = OLS(residuals, exog).fit()
    lm_stat, p_val, _, _ = het_breuschpagan(model.resid, exog)
    is_hetero = bool(p_val < alpha)
    if is_hetero:
        note = (
            f"Heteroscedasticidad detectada (p={p_val:.4f} < {alpha}). "
            "Los intervalos simetricos del modelo no son validos uniformemente. "
            "Recomendado: Conformal Prediction para bandas garantizadas."
        )
    else:
        note = (
            f"Sin evidencia de heteroscedasticidad (p={p_val:.4f}). "
            "Bandas simetricas (mean +/- 1.96*std) son razonables."
        )
    return HeteroscedasticityTest(
        lm_stat=float(lm_stat), p_value=float(p_val),
        is_heteroscedastic=is_hetero, note=note,
    )


def conformal_intervals(
    oof_residuals: np.ndarray,
    predictions: np.ndarray,
    alpha: float = 0.05,
) -> tuple[np.ndarray, np.ndarray]:
    """Split conformal prediction (Vovk et al., Lei et al.).

    Devuelve `(y_low, y_high)` con garantia estadistica:
        P(y_real in [y_low, y_high]) >= 1 - alpha
    bajo el supuesto de intercambiabilidad de las muestras (mas debil que
    i.i.d.; cumple OOF predictions).

    Implementacion: cuantil empirico de los residuos absolutos OOF, ajustado
    por finite-sample correction `(1-alpha)*(n+1)/n`. Esto es el split
    conformal mas simple, NO asume homoscedasticidad. Reemplaza las bandas
    heuristicas `mean +/- 1.96*std` (que solo serian validas si el modelo
    fuese homoscedastico Y con errores gaussianos).

    Limitacion: bandas SIMETRICAS y CONSTANTES en magnitud (mismo q para
    todas las predicciones). Si hay heteroscedasticidad fuerte, considerar
    extension Localized/CQR (futuro). Este split conformal sigue siendo
    valido marginalmente (cobertura promedio 1-alpha) aunque no
    condicionalmente por bin de prediccion.
    """
    abs_resid = np.abs(np.asarray(oof_residuals, dtype=float))
    abs_resid = abs_resid[np.isfinite(abs_resid)]
    n = len(abs_resid)
    predictions = np.asarray(predictions, dtype=float)

    if n < 20:
        # Muestra insuficiente para calibrar: devuelve bandas vacias (lo=hi=pred).
        return predictions.copy(), predictions.copy()

    level = min(1.0, (1 - alpha) * (n + 1) / n)
    q = float(np.quantile(abs_resid, level))
    return predictions - q, predictions + q


def calibration_bins(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    n_bins: int = 10,
) -> Optional[pd.DataFrame]:
    """Bin de predicciones + media de reales por bin, para calibration plot.

    Devuelve DataFrame con columnas:
      - bin_idx        : 0..n_bins-1
      - bin_pred_mean  : media de y_pred en el bin
      - bin_real_mean  : media de y_true en el bin
      - bin_count      : n filas en el bin
      - bin_diff_pct   : (real - pred) / pred * 100 (sesgo del bin)

    Un modelo perfectamente calibrado tiene `bin_real_mean ~ bin_pred_mean`
    en todos los bins. Sesgo positivo en bin alto = modelo subestima
    predicciones grandes (problema clasico en regresion con cola larga).
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    if y_true.size < n_bins * 2:
        return None

    bins = pd.qcut(y_pred, q=n_bins, labels=False, duplicates="drop")
    df = pd.DataFrame({"y_true": y_true, "y_pred": y_pred, "bin": bins})
    out = df.groupby("bin", observed=True).agg(
        bin_pred_mean=("y_pred", "mean"),
        bin_real_mean=("y_true", "mean"),
        bin_count=("y_true", "size"),
    ).reset_index().rename(columns={"bin": "bin_idx"})
    out["bin_diff_pct"] = np.where(
        out["bin_pred_mean"] != 0,
        (out["bin_real_mean"] - out["bin_pred_mean"]) / out["bin_pred_mean"] * 100,
        np.nan,
    )
    return out

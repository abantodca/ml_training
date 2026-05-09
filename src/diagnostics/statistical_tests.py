"""Wrappers tipados sobre statsmodels/scipy.

Cada funcion devuelve un dataclass `TestResult` con:
    name      : nombre del test
    statistic : valor de la estadistica
    p_value   : p-value (None si el test no produce p)
    rejects_h0 : True si rechaza H0 al nivel alpha (default 0.05)
    h0_meaning : descripcion legible de H0 ("residuos son homocedasticos", ...)
    notes      : info adicional (parametros, advertencias)

Patron defensivo: TODOS los tests devuelven `TestResult` aunque fallen.
Si scipy/statsmodels lanza por n insuficiente o singularidad, se captura
y se devuelve un resultado con `statistic=None` y un nota explicativa.
Esto permite que el HTML report siempre tenga datos que mostrar, marcando
los tests fallidos sin que el script entero abort.

Convención de p-value:
    Tests donde RECHAZAR significa "hay problema" (heterocedasticidad,
    autocorrelacion, no-normalidad): rejects_h0=True -> 🔴 hallazgo.
    Tests donde RECHAZAR significa "OK" (estacionariedad ADF):
    rejects_h0=True -> 🟢 OK.
    El campo `is_finding` lo declara cada wrapper para que el renderer
    pinte el badge correcto sin tener que conocer cada test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class TestResult:
    name: str
    statistic: Optional[float] = None
    p_value: Optional[float] = None
    rejects_h0: Optional[bool] = None
    h0_meaning: str = ""
    is_finding: bool = False  # True si rechazar H0 = "problema a remediar"
    notes: str = ""
    extra: dict = field(default_factory=dict)

    def status_emoji(self) -> str:
        """Devuelve 🟢/🔴/⚪ segun el resultado del test.

        ⚪ = test fallo o n insuficiente.
        🔴 = test arrojo hallazgo accionable (segun is_finding semantics).
        🟢 = test paso sin hallazgo.
        """
        if self.statistic is None or self.rejects_h0 is None:
            return "⚪"
        if self.is_finding:
            return "🔴" if self.rejects_h0 else "🟢"
        return "🟢" if self.rejects_h0 else "🔴"


def _safe_test(name: str, h0: str, is_finding: bool) -> TestResult:
    """Constructor de fallback para tests fallidos."""
    return TestResult(name=name, h0_meaning=h0, is_finding=is_finding,
                      notes="test no aplicable o fallo (n insuficiente / singular)")


# ---------------------------------------------------------------------------
# Normalidad (univariado)
# ---------------------------------------------------------------------------
def shapiro_wilk(x: pd.Series, alpha: float = 0.05) -> TestResult:
    """Shapiro-Wilk normality test. H0: sample viene de distribucion normal.

    Solo aplicable para n entre 3 y 5000. Si fuera de rango, devuelve
    fallback (preferir Anderson-Darling para n>5000).
    """
    from scipy.stats import shapiro

    name = "Shapiro-Wilk"
    h0 = "la muestra viene de una distribucion normal"
    x_clean = x.dropna()
    n = len(x_clean)
    if n < 3 or n > 5000:
        out = _safe_test(name, h0, is_finding=True)
        out.notes = f"n={n} fuera de rango [3, 5000]; usar Anderson-Darling"
        return out
    try:
        stat, p = shapiro(x_clean)
        return TestResult(
            name=name, statistic=float(stat), p_value=float(p),
            rejects_h0=p < alpha, h0_meaning=h0, is_finding=True,
            notes=f"n={n}",
        )
    except Exception as e:
        out = _safe_test(name, h0, is_finding=True)
        out.notes = f"error: {e}"
        return out


def anderson_darling(x: pd.Series, alpha: float = 0.05) -> TestResult:
    """Anderson-Darling normality test. Devuelve estadistica y p estimado."""
    from scipy.stats import anderson

    name = "Anderson-Darling"
    h0 = "la muestra viene de una distribucion normal"
    x_clean = x.dropna()
    if len(x_clean) < 8:
        return _safe_test(name, h0, is_finding=True)
    try:
        result = anderson(x_clean, dist="norm")
        # critical values son arrays, mapeamos al alpha mas cercano
        sig_levels = list(result.significance_level)  # [15.0, 10.0, 5.0, 2.5, 1.0]
        crit_at_alpha = result.critical_values[sig_levels.index(alpha * 100)]
        rejects = result.statistic > crit_at_alpha
        return TestResult(
            name=name, statistic=float(result.statistic), p_value=None,
            rejects_h0=bool(rejects), h0_meaning=h0, is_finding=True,
            notes=f"critical(α={alpha})={crit_at_alpha:.3f}",
            extra={"critical_values": list(result.critical_values),
                   "significance_level": sig_levels},
        )
    except Exception as e:
        out = _safe_test(name, h0, is_finding=True)
        out.notes = f"error: {e}"
        return out


def jarque_bera(x: pd.Series, alpha: float = 0.05) -> TestResult:
    """Jarque-Bera test. H0: skew=0 y kurtosis=3 (normal)."""
    from scipy.stats import jarque_bera as jb

    name = "Jarque-Bera"
    h0 = "skew=0 y excess_kurtosis=0 (normal)"
    x_clean = x.dropna()
    if len(x_clean) < 20:
        return _safe_test(name, h0, is_finding=True)
    try:
        stat, p = jb(x_clean)
        return TestResult(
            name=name, statistic=float(stat), p_value=float(p),
            rejects_h0=p < alpha, h0_meaning=h0, is_finding=True,
            notes=f"n={len(x_clean)} | skew={x_clean.skew():.3f} kurt={x_clean.kurtosis():.3f}",
        )
    except Exception as e:
        out = _safe_test(name, h0, is_finding=True)
        out.notes = f"error: {e}"
        return out


# ---------------------------------------------------------------------------
# Heterocedasticidad
# ---------------------------------------------------------------------------
def breusch_pagan(y: pd.Series, X: pd.DataFrame, alpha: float = 0.05) -> TestResult:
    """Breusch-Pagan test. H0: var(residuos|X) = constante (homocedasticidad).

    Ajusta OLS y(X) y testea heterocedasticidad sobre los residuos.
    """
    from statsmodels.regression.linear_model import OLS
    from statsmodels.stats.diagnostic import het_breuschpagan
    from statsmodels.tools import add_constant

    name = "Breusch-Pagan"
    h0 = "varianza de residuos es constante (homocedasticidad)"
    df = pd.concat([y, X], axis=1).dropna()
    if len(df) < 30:
        return _safe_test(name, h0, is_finding=True)
    try:
        y_clean = df[y.name]
        X_clean = add_constant(df[X.columns], has_constant="add")
        ols = OLS(y_clean.values, X_clean.values).fit()
        lm, lm_p, f, f_p = het_breuschpagan(ols.resid, X_clean.values)
        return TestResult(
            name=name, statistic=float(lm), p_value=float(lm_p),
            rejects_h0=lm_p < alpha, h0_meaning=h0, is_finding=True,
            notes=f"n={len(df)}, F={f:.2f}, F_p={f_p:.4f}",
            extra={"lm_statistic": float(lm), "lm_pvalue": float(lm_p),
                   "f_statistic": float(f), "f_pvalue": float(f_p)},
        )
    except Exception as e:
        out = _safe_test(name, h0, is_finding=True)
        out.notes = f"error: {e}"
        return out


def white_test(y: pd.Series, X: pd.DataFrame, alpha: float = 0.05) -> TestResult:
    """White test. Version mas robusta de heterocedasticidad (no asume forma)."""
    from statsmodels.regression.linear_model import OLS
    from statsmodels.stats.diagnostic import het_white
    from statsmodels.tools import add_constant

    name = "White (heteroscedasticity)"
    h0 = "varianza de residuos es constante (homocedasticidad)"
    df = pd.concat([y, X], axis=1).dropna()
    # White requiere n > k(k+3)/2 + 1, conservador: n >= 50
    if len(df) < 50:
        return _safe_test(name, h0, is_finding=True)
    try:
        y_clean = df[y.name]
        X_clean = add_constant(df[X.columns], has_constant="add")
        ols = OLS(y_clean.values, X_clean.values).fit()
        lm, lm_p, f, f_p = het_white(ols.resid, X_clean.values)
        return TestResult(
            name=name, statistic=float(lm), p_value=float(lm_p),
            rejects_h0=lm_p < alpha, h0_meaning=h0, is_finding=True,
            notes=f"n={len(df)}",
            extra={"lm_statistic": float(lm), "lm_pvalue": float(lm_p)},
        )
    except Exception as e:
        out = _safe_test(name, h0, is_finding=True)
        out.notes = f"error: {e}"
        return out


# ---------------------------------------------------------------------------
# Autocorrelacion
# ---------------------------------------------------------------------------
def durbin_watson(residuals: pd.Series) -> TestResult:
    """Durbin-Watson sobre residuos.

    DW ∈ [0, 4]. ≈2 => no autocorr. <1.5 => positiva. >2.5 => negativa.
    No tiene p-value canónico; reportamos la estadistica y un veredicto
    cualitativo en `notes`.
    """
    from statsmodels.stats.stattools import durbin_watson as dw

    name = "Durbin-Watson"
    h0 = "no hay autocorrelacion de primer orden en residuos"
    r = residuals.dropna()
    if len(r) < 10:
        return _safe_test(name, h0, is_finding=True)
    try:
        d = float(dw(r.values))
        if d < 1.5:
            verdict = "POSITIVA (modelo deja patron temporal)"
            rejects = True
        elif d > 2.5:
            verdict = "NEGATIVA (rara, posible overfitting)"
            rejects = True
        else:
            verdict = "no concluyente / OK"
            rejects = False
        return TestResult(
            name=name, statistic=d, p_value=None, rejects_h0=rejects,
            h0_meaning=h0, is_finding=True,
            notes=f"DW={d:.3f} → {verdict}",
        )
    except Exception as e:
        out = _safe_test(name, h0, is_finding=True)
        out.notes = f"error: {e}"
        return out


def ljung_box(residuals: pd.Series, lags: int = 10, alpha: float = 0.05) -> TestResult:
    """Ljung-Box (Q-test) sobre residuos. H0: no autocorrelacion hasta lag k."""
    from statsmodels.stats.diagnostic import acorr_ljungbox

    name = f"Ljung-Box (Q@{lags})"
    h0 = f"no hay autocorrelacion en residuos hasta lag {lags}"
    r = residuals.dropna()
    if len(r) < lags + 5:
        return _safe_test(name, h0, is_finding=True)
    try:
        result = acorr_ljungbox(r, lags=[lags], return_df=True)
        stat = float(result["lb_stat"].iloc[-1])
        p = float(result["lb_pvalue"].iloc[-1])
        return TestResult(
            name=name, statistic=stat, p_value=p,
            rejects_h0=p < alpha, h0_meaning=h0, is_finding=True,
            notes=f"n={len(r)}, lags={lags}",
        )
    except Exception as e:
        out = _safe_test(name, h0, is_finding=True)
        out.notes = f"error: {e}"
        return out


# ---------------------------------------------------------------------------
# Estacionariedad
# ---------------------------------------------------------------------------
def adf_test(x: pd.Series, alpha: float = 0.05) -> TestResult:
    """Augmented Dickey-Fuller. H0: existe raiz unitaria (NO estacionaria).

    Como rechazar H0 = "es estacionaria" = bueno, is_finding=False.
    """
    from statsmodels.tsa.stattools import adfuller

    name = "ADF (stationarity)"
    h0 = "la serie tiene raiz unitaria (NO estacionaria)"
    x_clean = x.dropna()
    if len(x_clean) < 30:
        return _safe_test(name, h0, is_finding=False)
    try:
        result = adfuller(x_clean.values, autolag="AIC")
        stat, p = float(result[0]), float(result[1])
        return TestResult(
            name=name, statistic=stat, p_value=p,
            rejects_h0=p < alpha, h0_meaning=h0, is_finding=False,
            notes=f"n={len(x_clean)}, lag_used={result[2]}",
            extra={"critical_values": dict(result[4])},
        )
    except Exception as e:
        out = _safe_test(name, h0, is_finding=False)
        out.notes = f"error: {e}"
        return out


def kpss_test(x: pd.Series, alpha: float = 0.05) -> TestResult:
    """KPSS. H0: la serie es estacionaria. Inverso al ADF, util como corroboracion."""
    from statsmodels.tsa.stattools import kpss

    name = "KPSS (stationarity)"
    h0 = "la serie es estacionaria"
    x_clean = x.dropna()
    if len(x_clean) < 30:
        return _safe_test(name, h0, is_finding=True)
    try:
        # warning suppression: KPSS ruidoso si p fuera de tabla
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            stat, p, lag, crit = kpss(x_clean.values, regression="c", nlags="auto")
        return TestResult(
            name=name, statistic=float(stat), p_value=float(p),
            rejects_h0=p < alpha, h0_meaning=h0, is_finding=True,
            notes=f"n={len(x_clean)}, lag={lag}",
            extra={"critical_values": dict(crit)},
        )
    except Exception as e:
        out = _safe_test(name, h0, is_finding=True)
        out.notes = f"error: {e}"
        return out

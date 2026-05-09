"""Entrypoint del modulo EDA.

Uso programatico:
    from src.diagnostics import run_eda
    out_path = run_eda("POP")
    print(out_path)  # reports/EDA_POP_2026-05-09_12-34.html

Uso CLI (dentro del container):
    docker compose run --rm --no-deps --entrypoint python trainer \\
      -m src.diagnostics.eda --variety POP

El proceso:
    1. Carga la data raw via `step_01_load.data_loader.load_data(sheet=variety)`.
    2. Genera profile univariado por cada NUMERIC_FEATURE + TARGET.
    3. Genera profile temporal por TARGET (y opcionalmente top features).
    4. Calcula multivariado (correlation, VIF, MI con target).
    5. Calcula drift PSI por anio para todas las numericas.
    6. Sintetiza hallazgos en regla-based: top 5 con severity.
    7. Renderiza HTML en `reports/EDA_<variety>_<ts>.html`.
    8. Si hay un MLflow run activo, sube el HTML como artifact.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

import pandas as pd

from src.config import (
    CATEGORICAL_FEATURES,
    DATE_COLUMN,
    NUMERIC_FEATURES,
    REPORTS_DIR,
    TARGET,
    init_dirs,
)
from src.diagnostics.distributions import VariableProfile, profile_all_numeric
from src.diagnostics.html_renderer import render_eda_html, write_eda_html
from src.diagnostics.multivariate import (
    compute_mutual_information,
    compute_vif,
    correlation_matrix,
)
from src.diagnostics.plots import (
    acf_pacf_bars,
    boxplot_by_group,
    correlation_heatmap,
    histogram_with_kde,
    mi_bars,
    psi_heatmap,
    qq_plot,
    vif_bars,
)
from src.diagnostics.temporal import (
    DriftReport,
    TemporalProfile,
    drift_report,
    profile_temporal,
)
from src.step_01_load.data_loader import load_data

logger = logging.getLogger(__name__)


def _quality_metrics(df: pd.DataFrame) -> dict:
    """Metricas top-level de calidad de datos."""
    return {
        "n_rows": len(df),
        "n_cols_raw": df.shape[1],
        "n_duplicates": int(df.duplicated().sum()),
        "n_missing_total": int(df.isna().sum().sum()),
        "missing_ratio_global": f"{df.isna().mean().mean():.2%}",
        "n_unique_FUNDO": int(df["FUNDO"].nunique()) if "FUNDO" in df.columns else 0,
        "n_unique_FORMATO": int(df["FORMATO"].nunique()) if "FORMATO" in df.columns else 0,
        "fecha_min": str(df[DATE_COLUMN].min()) if DATE_COLUMN in df.columns else "—",
        "fecha_max": str(df[DATE_COLUMN].max()) if DATE_COLUMN in df.columns else "—",
    }


def _synthesize_findings(
    var_profiles: List[VariableProfile],
    target_temporal: TemporalProfile,
    high_corr_pairs: List[tuple],
    vif_results,
    drift_reports: List[DriftReport],
) -> List[Tuple[str, str]]:
    """Reglas heuristicas que producen los hallazgos top.

    Devuelve [(severity, message)] con severity in {"high", "medium", "low", "good"}.
    """
    findings: List[Tuple[str, str]] = []

    # Heteroscedasticidad latente: variables muy skewed o con kurt alta
    for p in var_profiles:
        if abs(p.skew) > 1.5 or abs(p.kurtosis) > 5:
            sev = "high" if abs(p.skew) > 3 or abs(p.kurtosis) > 10 else "medium"
            findings.append((
                sev,
                f"{p.name} muestra distribucion fuertemente sesgada "
                f"(skew={p.skew:+.2f}, kurt={p.kurtosis:+.2f}). "
                f"Recomendacion: {p.boxcox_recommendation}.",
            ))

    # Outliers altos
    for p in var_profiles:
        if p.n > 0 and p.n_outliers_iqr / max(p.n, 1) > 0.05:
            findings.append((
                "medium",
                f"{p.name} tiene {p.n_outliers_iqr} outliers IQR "
                f"({p.n_outliers_iqr / p.n:.1%}). "
                f"OutlierCapper actual deberia manejarlos; revisar bounds por FUNDO.",
            ))

    # Autocorrelacion del target sin remover (estacional)
    if target_temporal.durbin_watson.statistic is not None:
        dw = target_temporal.durbin_watson.statistic
        if dw < 1.5:
            findings.append((
                "high",
                f"Target {TARGET} presenta autocorrelacion positiva fuerte (DW={dw:.2f}). "
                "Confirma necesidad de lag features y CV temporal honesto.",
            ))
        elif dw > 2.5:
            findings.append((
                "medium",
                f"Target {TARGET} presenta autocorrelacion negativa (DW={dw:.2f}). "
                "Posible sobre-dispersion; revisar transformacion del target.",
            ))

    # Estacionariedad del target
    adf = target_temporal.adf
    kpss = target_temporal.kpss
    if adf.rejects_h0 and kpss.rejects_h0 is False:
        findings.append((
            "good",
            f"Target {TARGET} estacionaria (ADF rechaza, KPSS no rechaza).",
        ))
    elif adf.rejects_h0 is False and kpss.rejects_h0:
        findings.append((
            "high",
            f"Target {TARGET} NO estacionaria (ADF no rechaza, KPSS rechaza). "
            "Considerar diferenciacion o detrending si los lag features no alcanzan.",
        ))

    # Multicolinealidad alta
    high_vif = [r for r in vif_results if r.severity == "high"]
    if high_vif:
        top = ", ".join(r.feature for r in high_vif[:5])
        findings.append((
            "high",
            f"{len(high_vif)} features con VIF>10 (multicolinealidad severa). Top: {top}.",
        ))

    # Correlation pairs >0.95 (redundancia casi perfecta)
    very_high = [(a, b, r) for a, b, r in high_corr_pairs if abs(r) >= 0.95]
    if very_high:
        findings.append((
            "medium",
            f"{len(very_high)} pares de features con |corr| ≥ 0.95 — redundancia casi total.",
        ))

    # Drift severo
    severe_drift = [r for r in drift_reports if r.drift_severity == "severo"]
    if severe_drift:
        names = ", ".join(r.variable for r in severe_drift[:5])
        findings.append((
            "high",
            f"{len(severe_drift)} variables con PSI > 0.25 entre anios consecutivos: {names}. "
            "Inestabilidad temporal — revisar drift / cambio de regimen.",
        ))

    # Si no hay hallazgos, mensaje positivo
    if not findings:
        findings.append((
            "good",
            "Sin hallazgos criticos automaticos. Revisar tarjetas detalladas para nuances.",
        ))

    # Cap a top 8 ordenados por severidad
    severity_order = {"high": 0, "medium": 1, "low": 2, "good": 3}
    findings.sort(key=lambda f: severity_order.get(f[0], 9))
    return findings[:8]


def run_eda(variety: str, out_dir: Path | None = None,
            *, log_to_mlflow: bool = True) -> Path:
    """Ejecuta el EDA completo para una variedad y devuelve el path al HTML."""
    init_dirs()
    out_dir = out_dir or REPORTS_DIR
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    out_path = out_dir / f"EDA_{variety}_{ts}.html"

    # ---- 1. Carga raw ----
    logger.info(f"[EDA/{variety}] cargando data raw...")
    X, y = load_data(sheet=variety)
    df = X.copy()
    df[TARGET] = y.values
    if DATE_COLUMN in df.columns:
        df = df.sort_values(DATE_COLUMN).reset_index(drop=True)

    # ---- 2. Calidad ----
    quality = _quality_metrics(df)

    # ---- 3. Univariado ----
    numeric_cols = [c for c in NUMERIC_FEATURES if c in df.columns] + [TARGET]
    logger.info(f"[EDA/{variety}] univariado sobre {len(numeric_cols)} variables...")
    var_profiles = profile_all_numeric(df, numeric_cols)
    var_profiles_with_figs = [
        (
            p,
            histogram_with_kde(df[p.name], p.name),
            qq_plot(df[p.name], p.name),
            boxplot_by_group(df, p.name, "FUNDO", p.name)
            if "FUNDO" in df.columns else
            boxplot_by_group(df, p.name, CATEGORICAL_FEATURES[0], p.name),
        )
        for p in var_profiles
    ]

    # ---- 4. Temporal del target ----
    logger.info(f"[EDA/{variety}] temporal sobre target...")
    target_series = df[TARGET]
    target_temporal = profile_temporal(TARGET, target_series, period=12)
    temporal_profiles_with_figs = [(
        target_temporal,
        acf_pacf_bars(target_temporal.acf, target_temporal.pacf,
                      n=len(target_series.dropna()), name=TARGET),
    )]
    # Tambien temporal de top-3 numericas con mayor varianza
    top_var_cols = sorted(
        [(c, df[c].var()) for c in NUMERIC_FEATURES if c in df.columns],
        key=lambda t: t[1] or 0, reverse=True,
    )[:3]
    for c, _ in top_var_cols:
        prof = profile_temporal(c, df[c], period=12)
        temporal_profiles_with_figs.append((
            prof,
            acf_pacf_bars(prof.acf, prof.pacf, n=len(df[c].dropna()), name=c),
        ))

    # ---- 5. Multivariado ----
    logger.info(f"[EDA/{variety}] multivariado...")
    numeric_only = df[[c for c in numeric_cols if c != TARGET]]
    corr = correlation_matrix(numeric_only, method="spearman", high_threshold=0.85)
    vif_results = compute_vif(numeric_only)
    mi_results = compute_mutual_information(numeric_only, df[TARGET])

    # ---- 6. Drift por anio ----
    logger.info(f"[EDA/{variety}] drift entre anios...")
    drift_reports = [
        drift_report(df, c, year_col="ANIO", date_col=DATE_COLUMN)
        for c in numeric_cols if c != TARGET
    ]
    drift_reports = [r for r in drift_reports if r.year_pairs]

    # ---- 7. Findings ----
    findings = _synthesize_findings(
        var_profiles=var_profiles,
        target_temporal=target_temporal,
        high_corr_pairs=corr.high_pairs,
        vif_results=vif_results,
        drift_reports=drift_reports,
    )

    # ---- 8. Render ----
    html = render_eda_html(
        variety=variety,
        n_rows=len(df),
        n_cols=df.shape[1],
        quality_metrics=quality,
        findings=findings,
        var_profiles_with_figs=var_profiles_with_figs,
        temporal_profiles_with_figs=temporal_profiles_with_figs,
        corr_fig=correlation_heatmap(corr.columns, corr.matrix, corr.method),
        vif_fig=vif_bars(vif_results),
        mi_fig=mi_bars(mi_results),
        psi_fig=psi_heatmap(drift_reports),
        high_corr_pairs=corr.high_pairs,
    )

    write_eda_html(html, out_path)
    logger.info(f"[EDA/{variety}] HTML generado: {out_path}")

    # ---- 9. MLflow artifact si hay run activo ----
    if log_to_mlflow:
        try:
            import mlflow
            if mlflow.active_run() is not None:
                mlflow.log_artifact(str(out_path), artifact_path="eda")
                logger.info(f"[EDA/{variety}] subido como artifact a MLflow")
        except Exception as exc:
            logger.warning(f"[EDA/{variety}] log_artifact MLflow fallo: {exc}")

    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="EDA diagnostico standalone")
    p.add_argument("--variety", required=True, help="Variedad (hoja del Excel)")
    p.add_argument("--out-dir", default=None, help="Override del directorio de salida")
    p.add_argument("--no-mlflow", action="store_true",
                   help="No intenta subir como artifact a MLflow")
    return p.parse_args()


def _main() -> int:
    from src.utils.logger import setup_logging
    setup_logging()
    args = _parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else None
    out_path = run_eda(
        args.variety, out_dir=out_dir,
        log_to_mlflow=not args.no_mlflow,
    )
    print(f"\nEDA report: {out_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

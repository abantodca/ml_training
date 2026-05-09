"""Dashboard Ejecutivo del modelo ganador (un solo HTML por variedad).

Orquestador thin: arma el `WinnerKit` (kit ejecutivo derivado del campeon)
y ensambla las 6 secciones del dashboard en un HTML estandalone:

  1. Hero        : veredicto + descarga Excel.
  2. Context     : ¿Que datos uso?
  3. Mega KPIs   : ¿Que tan preciso? ¿Cuanto explica? ¿Vale la pena?
  4. Guide       : ¿Como leer este reporte? + glosario.
  5. Actions     : ¿Que hacer hoy? (auto-generado).
  6. Technical   : detalle DS (colapsable).

CSS / plotly bundle viven en `styles.py`; helpers de presentacion en
`helpers.py`; secciones en `sections.py` y `technical.py`. El kit
ejecutivo vive en `step_05_evaluate.explainability.build_winner_kit`
para que el Excel ejecutivo lo reuse sin duplicar logica.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
from typing import List, Optional

import pandas as pd

from src.config import REPORT_PROJECT_NAME, REPORTS_DIR
from src.step_05_evaluate.champion import ModelResult
from src.step_05_evaluate.explainability import build_winner_kit
from src.step_05_evaluate.html.sections import (
    build_actions_section,
    build_backends_comparison_section,
    build_bias_section,
    build_context_section,
    build_diagnostic_links_section,
    build_errors_detail_section,
    build_guide_section,
    build_hero,
    build_mega_kpis,
    build_pdp_section,
    build_statistical_diagnostic_section,
)
from src.step_05_evaluate.html.styles import DASHBOARD_CSS, _PLOTLY_JS_TAG
from src.step_05_evaluate.html.technical import build_technical_section


def render_winner_dashboard(
    *,
    variety: str,
    results: List[ModelResult],
    champion: ModelResult,
    decision: dict,
    output_dir: Optional[Path] = None,
    excel_path: Optional[str] = None,
    X_raw: Optional[pd.DataFrame] = None,
) -> Path:
    """Genera `reports/Winner_{variety}.html` y devuelve la ruta."""
    out_dir = Path(output_dir) if output_dir else REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"Winner_{variety}.html"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    kit = build_winner_kit(variety=variety, champion=champion, X_raw=X_raw)

    hero = build_hero(
        variety=variety, champion=champion, verdict=kit.verdict,
        excel_path=excel_path, timestamp=ts,
    )
    context_html = build_context_section(kit.context, champion)
    mega_kpis = build_mega_kpis(
        kit.abs_err, kit.real, kit.pred, kit.oof_mape, kit.oof_r2,
    )
    guide = build_guide_section()
    backends_compare = build_backends_comparison_section(results, champion)
    stat_diag = build_statistical_diagnostic_section(
        mae_ci=kit.mae_oof_ci, mape_ci=kit.mape_oof_ci, r2_ci=kit.r2_oof_ci,
        heteroscedasticity=kit.heteroscedasticity,
        calibration_df=kit.calibration,
    )
    bias_html = build_bias_section(kit.fundo_bias)
    errors_detail_html = build_errors_detail_section(
        business_validation=champion.business_validation,
        X_aligned=kit.X_aligned,
        excel_path=excel_path,
    )
    actions_html = build_actions_section(kit.actions)

    # PDP (Partial Dependence Plots): defensivo, requiere cargar pipeline +
    # transformar X_sample. Si cualquier paso falla, la seccion se omite.
    pdp_html = ""
    if X_raw is not None and champion.pipeline_path:
        try:
            import joblib
            ensemble = joblib.load(champion.pipeline_path)
            # Tomamos el primer pipeline del ensemble para preprocessor + features
            inner_pipe = ensemble.models_[0] if hasattr(ensemble, "models_") else ensemble
            preprocessor = inner_pipe.named_steps["preprocessor"]
            X_sample = X_raw.head(min(500, len(X_raw)))
            X_transformed = preprocessor.transform(X_sample)
            from src.step_05_evaluate.diagnostics import plot_partial_dependence_plotly
            pdp_html = plot_partial_dependence_plotly(
                ensemble, X_transformed,
                feature_names=list(X_transformed.columns),
                top_k=5,
            )
        except Exception:
            pdp_html = ""
    pdp_section = build_pdp_section(pdp_html)
    diagnostic_links = build_diagnostic_links_section(variety, out_dir)
    technical = build_technical_section(
        results=results, champion=champion, decision=decision,
        X_aligned=kit.X_aligned, abs_errors=kit.abs_err, real=kit.real,
    )

    html = f"""<!doctype html>
        <html lang="es"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Winner · {escape(variety)} — {escape(REPORT_PROJECT_NAME)}</title>
        {_PLOTLY_JS_TAG}
        <style>{DASHBOARD_CSS}</style></head>
        <body>
        <div class="wrap">
        {hero}
        {context_html}
        {mega_kpis}
        {guide}
        {backends_compare}
        {stat_diag}
        {pdp_section}
        {bias_html}
        {errors_detail_html}
        {diagnostic_links}
        {actions_html}
        {technical}
        <footer>Generado automáticamente por el pipeline de entrenamiento ML · {escape(ts)}</footer>
        </div>
        </body></html>
    """

    out_path.write_text(html, encoding="utf-8")
    return out_path

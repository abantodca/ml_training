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
from src.step_05_evaluate.feature_importance import FeatureImportanceResult
from src.step_05_evaluate.html.sections import (
    build_actions_section,
    build_context_section,
    build_feature_importance_section,
    build_guide_section,
    build_hero,
    build_mega_kpis,
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
    feature_importance: Optional[FeatureImportanceResult] = None,
) -> Path:
    """Genera `reports/Winner_{variety}.html` y devuelve la ruta."""
    out_dir = Path(output_dir) if output_dir else REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"Winner_{variety}.html"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    kit = build_winner_kit(
        variety=variety, champion=champion, X_raw=X_raw,
        feature_importance=feature_importance,
    )

    hero = build_hero(
        variety=variety, champion=champion, verdict=kit.verdict,
        excel_path=excel_path, timestamp=ts, stacking=kit.stacking,
    )
    context_html = build_context_section(kit.context, champion, stacking=kit.stacking)
    mega_kpis = build_mega_kpis(
        kit.abs_err, kit.real, kit.pred, kit.oof_mape, kit.oof_r2,
        stacking=kit.stacking,
    )
    guide = build_guide_section()
    actions_html = build_actions_section(kit.actions)
    fi_html = build_feature_importance_section(kit.feature_importance)
    technical = build_technical_section(
        results=results, champion=champion, decision=decision,
        X_aligned=kit.X_aligned, abs_errors=kit.abs_err, real=kit.real,
        stacking=kit.stacking,
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
{actions_html}
{fi_html}
{technical}
<footer>Generado automáticamente por el pipeline de entrenamiento ML · {escape(ts)}</footer>
</div>
</body></html>"""

    out_path.write_text(html, encoding="utf-8")
    return out_path

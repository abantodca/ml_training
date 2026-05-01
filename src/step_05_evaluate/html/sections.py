"""Secciones ejecutivas del dashboard (top-down):

  1. Hero        : veredicto + descarga Excel.
  2. Context     : contexto del dataset (filas, fechas, fundos, formatos).
  3. Mega KPIs   : las 3 preguntas que importan en lenguaje simple.
  4. Guide       : ¿Como leer este reporte? + glosario.
  5. Actions     : recomendaciones auto-generadas.

Cada `build_*` recibe primitivos / dataclasses listos (no hace I/O ni
calculos pesados) y devuelve un fragmento HTML escapado.
"""
from __future__ import annotations

from html import escape
from typing import List, Optional

import numpy as np

from src.config import (
    REPORT_BUSINESS_UNIT,
    REPORT_MODEL_DESCRIPTION,
    REPORT_PROJECT_NAME,
)
from src.step_05_evaluate.champion import ModelResult
from src.step_05_evaluate.explainability import (
    Action,
    PlainKPI,
    TrainingContext,
    Verdict,
    glossary_terms,
    kpi_explanatory_power,
    kpi_precision,
    kpi_vs_baseline,
)
from src.step_05_evaluate.html.helpers import download_button


def build_hero(
    *,
    variety: str,
    champion: ModelResult,
    verdict: Verdict,
    excel_path: Optional[str],
    timestamp: str,
) -> str:
    return f"""
    <div class="hero {verdict.color_key}">
      <div class="hero-text">
        <div class="eyebrow">Dashboard ejecutivo · Modelo de productividad</div>
        <h1>{escape(REPORT_PROJECT_NAME)}</h1>
        <div class="meta">
          Variedad <b>{escape(variety)}</b> · Modelo <b>{escape(champion.model_type.upper())}</b>
          · {escape(REPORT_BUSINESS_UNIT)} · {escape(timestamp)}
        </div>
        <div class="verdict-badge">
          <span class="icon">{verdict.icon}</span>
          <span>{escape(verdict.title)}</span>
        </div>
        <div class="verdict-headline">{escape(verdict.headline)}</div>
        <div class="verdict-body">{escape(verdict.body)}</div>
      </div>
      <div class="hero-side">
        {download_button(excel_path, variety)}
      </div>
    </div>
    """


def build_context_section(ctx: TrainingContext, champion: ModelResult) -> str:
    date_range = "—"
    if ctx.date_min and ctx.date_max:
        date_range = f"{ctx.date_min} a {ctx.date_max}"

    fundos_str = ", ".join(ctx.fundos_top) if ctx.fundos_top else "—"
    formatos_str = ", ".join(ctx.formatos_top[:3]) if ctx.formatos_top else "—"
    if ctx.n_formatos > 3:
        formatos_str += f", +{ctx.n_formatos - 3} más"

    cards = "".join([
        f'<div class="ctx-card"><div class="label">Cosechas analizadas</div>'
        f'<div class="value">{ctx.n_rows:,}</div>'
        f'<div class="sub">filas del histórico</div></div>',
        f'<div class="ctx-card"><div class="label">Período cubierto</div>'
        f'<div class="value">{escape(date_range)}</div>'
        f'<div class="sub">rango de fechas</div></div>',
        f'<div class="ctx-card"><div class="label">Fundos</div>'
        f'<div class="value">{ctx.n_fundos}</div>'
        f'<div class="sub">{escape(fundos_str)}</div></div>',
        f'<div class="ctx-card"><div class="label">Formatos</div>'
        f'<div class="value">{ctx.n_formatos}</div>'
        f'<div class="sub">{escape(formatos_str)}</div></div>',
        f'<div class="ctx-card"><div class="label">Modelo elegido</div>'
        f'<div class="value">{escape(champion.model_type.upper())}</div>'
        f'<div class="sub">de {champion.elapsed_seconds:.0f}s entrenamiento</div></div>',
    ])
    return f"""
    <section>
      <div class="eyebrow">Contexto del entrenamiento</div>
      <h2>¿Qué datos usó este modelo?</h2>
      <p class="lead">{escape(REPORT_MODEL_DESCRIPTION)}</p>
      <div class="ctx-grid">{cards}</div>
    </section>
    """


def _kpi_mega_card(kpi: PlainKPI, icon: str) -> str:
    score_pill = ""
    if kpi.score_label not in ("—", ""):
        score_pill = (
            f'<span class="score-pill {escape(kpi.score_label)}">'
            f'{escape(kpi.score_label)}</span>'
        )
    return f"""
    <div class="kpi-mega">
      <div class="question">
        <span>{icon}</span>
        <span>{escape(kpi.question)}</span>
        {score_pill}
      </div>
      <div class="headline">{escape(kpi.headline)}</div>
      <div class="detail">{escape(kpi.detail)}</div>
      <div class="technical">{escape(kpi.technical)}</div>
    </div>
    """


def build_mega_kpis(
    abs_errors: np.ndarray,
    real: np.ndarray,
    pred: np.ndarray,
    full_mape: float,
    full_r2: Optional[float],
) -> str:
    k1 = kpi_precision(abs_errors, full_mape)
    k2 = kpi_explanatory_power(full_r2)
    k3 = kpi_vs_baseline(real, pred)
    cards = (
        _kpi_mega_card(k1, "🎯")
        + _kpi_mega_card(k2, "📊")
        + _kpi_mega_card(k3, "📈")
    )
    return f"""
    <section>
      <div class="eyebrow">Las preguntas que importan</div>
      <h2>¿Qué tan bueno es este modelo?</h2>
      <p class="lead">Tres respuestas en lenguaje simple. Si tienes 30 segundos para entender el modelo, lee esto.</p>
      <div class="kpi-mega-grid">{cards}</div>
    </section>
    """


def build_guide_section() -> str:
    rows = "".join(
        f'<div class="gloss-row"><div class="term">{escape(t)}</div>'
        f'<div class="def">{escape(d)}</div></div>'
        for t, d in glossary_terms()
    )
    return f"""
    <details class="help">
      <summary>📖 ¿Cómo leer este reporte?</summary>
      <div class="body">
        <p>Este dashboard responde 3 preguntas sobre un modelo de predicción
        de productividad por jornal. Está organizado de lo más simple a lo
        más detallado:</p>
        <ol>
          <li><b>Veredicto ejecutivo (arriba)</b>: una respuesta clara — ¿usar este modelo o no?</li>
          <li><b>Contexto</b>: con qué datos se entrenó.</li>
          <li><b>Las 3 preguntas que importan</b>: precisión, capacidad explicativa, valor vs no usar modelo.</li>
          <li><b>Acciones recomendadas</b>: qué hacer hoy con esta información.</li>
          <li><b>Detalle técnico</b> (colapsado): para el equipo de Data Science — comparación de modelos, gráficos de error, distribución por subgrupo.</li>
        </ol>
        <p style="margin-top:14px"><b>Glosario de términos técnicos:</b></p>
        <div class="glossary-grid">{rows}</div>
      </div>
    </details>
    """


def build_actions_section(actions: List[Action]) -> str:
    items = "".join(
        f'<div class="action {escape(a.severity)}">'
        f'<div class="icon">{a.icon}</div>'
        f'<div class="body-wrap">'
        f'<div class="title">{escape(a.title)}</div>'
        f'<div class="body">{escape(a.body)}</div>'
        f'</div></div>'
        for a in actions
    )
    return f"""
    <section>
      <div class="eyebrow">Acciones recomendadas</div>
      <h2>¿Qué hacer con esto hoy?</h2>
      <p class="lead">Recomendaciones generadas automáticamente del análisis de errores y subgrupos.</p>
      {items}
    </section>
    """

"""Secciones ejecutivas del dashboard (top-down):

  1. Hero        : veredicto + descarga Excel.
  2. Context     : contexto del dataset (filas, fechas, fundos, formatos).
  3. Mega KPIs   : las 3 preguntas que importan en lenguaje simple.
  4. Guide       : ¿Como leer este reporte? + glosario.
  5. Actions     : recomendaciones auto-generadas.
  6. Feature Importance : que variables sostienen el modelo (post-hoc).

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
from src.step_05_evaluate.feature_importance import (
    STATUS_CORE,
    STATUS_NOISE,
    STATUS_PRUNABLE,
    STATUS_UTIL,
    FeatureImportanceResult,
)
from src.step_05_evaluate.html.helpers import download_button
from src.step_05_evaluate.stacking_diagnostics import StackingDiagnostics


def _stacking_pill(stacking: Optional[StackingDiagnostics]) -> str:
    """Pill del hero — comunica en una mirada si la capa meta está ayudando.

    Tres estados visuales:
      - active + improves_base : verde, "Capa meta ACTIVA · -X% error"
      - active sin mejora      : amarillo, "Capa meta ACTIVA · cambio +X%"
      - fallback               : gris, "Capa meta FALLBACK · usa modelo base"
    Sin stacking devuelve string vacío (no contamina el hero del legacy).
    """
    if stacking is None:
        return ""
    if not stacking.active:
        cls = "fallback"
        sub = "usa modelo base · seguridad activada"
    elif stacking.improves_base:
        cls = "active"
        sub = f"reduce error en {abs(stacking.delta_pct):.2f}%"
    else:
        cls = "neutral"
        sub = f"cambio {stacking.delta_pct:+.2f}% en error"
    return (
        f'<div class="stacking-pill {cls}">'
        f'<span class="dot"></span>'
        f'<span class="text">'
        f'<span class="lbl">Capa meta {stacking.status_label}</span>'
        f'<span class="sub">{escape(sub)}</span>'
        f'</span>'
        f'</div>'
    )


def build_hero(
    *,
    variety: str,
    champion: ModelResult,
    verdict: Verdict,
    excel_path: Optional[str],
    timestamp: str,
    stacking: Optional[StackingDiagnostics] = None,
) -> str:
    """Hero ejecutivo + pill de stacking si el campeón usa capa meta."""
    pill = _stacking_pill(stacking)
    # Etiqueta de modelo: si hay stacking activo, mostrar "XGB + GAM"; si
    # hay fallback, "XGB (meta desactivada)"; si no hay stacking, solo XGB.
    if stacking is None:
        model_label = champion.model_type.upper()
    elif stacking.active:
        model_label = f"{champion.model_type.upper()} + {stacking.meta_type.upper()}"
    else:
        model_label = f"{champion.model_type.upper()} (meta desactivada)"
    return f"""
    <div class="hero {verdict.color_key}">
      <div class="hero-text">
        <div class="eyebrow">Dashboard ejecutivo · Modelo de productividad</div>
        <h1>{escape(REPORT_PROJECT_NAME)}</h1>
        <div class="meta">
          Variedad <b>{escape(variety)}</b> · Modelo <b>{escape(model_label)}</b>
          · {escape(REPORT_BUSINESS_UNIT)} · {escape(timestamp)}
        </div>
        <div class="verdict-badge">
          <span class="icon">{verdict.icon}</span>
          <span>{escape(verdict.title)}</span>
        </div>
        <div class="verdict-headline">{escape(verdict.headline)}</div>
        <div class="verdict-body">{escape(verdict.body)}</div>
        {pill}
      </div>
      <div class="hero-side">
        {download_button(excel_path, variety)}
      </div>
    </div>
    """


def build_context_section(
    ctx: TrainingContext,
    champion: ModelResult,
    stacking: Optional[StackingDiagnostics] = None,
) -> str:
    date_range = "—"
    if ctx.date_min and ctx.date_max:
        date_range = f"{ctx.date_min} a {ctx.date_max}"

    fundos_str = ", ".join(ctx.fundos_top) if ctx.fundos_top else "—"
    formatos_str = ", ".join(ctx.formatos_top[:3]) if ctx.formatos_top else "—"
    if ctx.n_formatos > 3:
        formatos_str += f", +{ctx.n_formatos - 3} más"

    if stacking is None:
        model_label = champion.model_type.upper()
        model_sub = f"de {champion.elapsed_seconds:.0f}s entrenamiento"
    elif stacking.active:
        model_label = f"{champion.model_type.upper()} + {stacking.meta_type.upper()}"
        model_sub = "modelo base + capa meta (stacking activo)"
    else:
        model_label = champion.model_type.upper()
        model_sub = "modelo base (capa meta desactivada por seguridad)"

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
        f'<div class="value">{escape(model_label)}</div>'
        f'<div class="sub">{escape(model_sub)}</div></div>',
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
    stacking: Optional[StackingDiagnostics] = None,
) -> str:
    k1 = kpi_precision(abs_errors, full_mape)
    k2 = kpi_explanatory_power(full_r2)
    k3 = kpi_vs_baseline(real, pred)
    cards = (
        _kpi_mega_card(k1, "🎯")
        + _kpi_mega_card(k2, "📊")
        + _kpi_mega_card(k3, "📈")
    )
    # Nota explícita sobre la fuente de los KPIs cuando hay stacking. Sin
    # ella, el lector asume que el "modelo" del que hablan los KPIs es el
    # mismo del hero — pero el OOF honesto se mide sobre el modelo base.
    footnote = ""
    if stacking is not None:
        if stacking.active and stacking.improves_base:
            footnote = (
                f'<p class="kpi-footnote">Las cifras de arriba son del modelo '
                f'base medido honestamente. La capa meta (activa) reduce el '
                f'error en {abs(stacking.delta_pct):.2f}% adicional según el '
                f'auto-diagnóstico interno — ver "Capa meta" en el detalle '
                f'técnico.</p>'
            )
        elif stacking.active:
            footnote = (
                f'<p class="kpi-footnote">Las cifras de arriba son del modelo '
                f'base medido honestamente. La capa meta está activa con un '
                f'cambio de {stacking.delta_pct:+.2f}% en el error.</p>'
            )
        else:
            footnote = (
                '<p class="kpi-footnote">El modelo en producción es el '
                'mostrado arriba: la capa meta no aportó mejora suficiente '
                'y el sistema la desactivó automáticamente (auto-fallback).</p>'
            )
    return f"""
    <section>
      <div class="eyebrow">Las preguntas que importan</div>
      <h2>¿Qué tan bueno es este modelo?</h2>
      <p class="lead">Tres respuestas en lenguaje simple. Si tienes 30 segundos para entender el modelo, lee esto.</p>
      <div class="kpi-mega-grid">{cards}</div>
      {footnote}
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


_FI_STATUS_LABELS: dict = {
    STATUS_CORE: ("Núcleo", "🟢"),
    STATUS_UTIL: ("Útil", "🟡"),
    STATUS_PRUNABLE: ("Podable", "🔴"),
    STATUS_NOISE: ("Ruido", "⚫"),
}

_FI_STATUS_DESCRIPTIONS: dict = {
    STATUS_CORE: (
        "Variables que sostienen el modelo (≥5% del impacto SHAP total). "
        "Sin ellas el error sube significativamente. NO eliminar."
    ),
    STATUS_UTIL: (
        "Variables que aportan entre 1% y 5% del impacto SHAP total. "
        "Mantener; eliminar solo si hay razón operativa (costo de captura)."
    ),
    STATUS_PRUNABLE: (
        "Variables con contribución entre 0.1% y 1% del impacto SHAP total. "
        "El modelo casi no las usa — eliminarlas simplifica el pipeline sin "
        "degradar el error."
    ),
    STATUS_NOISE: (
        "Variables con contribución <0.1% del impacto SHAP total. "
        "Aporte despreciable — PODAR PRIMERO."
    ),
}


def _direction_arrow(direction: float, threshold: float = 0.005) -> tuple[str, str]:
    """Devuelve (icono, css_class) segun el signo y magnitud del SHAP signed avg."""
    if abs(direction) < threshold:
        return ("≈", "neutral")
    if direction > 0:
        return ("↑", "up")
    return ("↓", "down")


def build_feature_importance_section(fi: Optional[FeatureImportanceResult]) -> str:
    """Sección SHAP de importancia de variables.

    Renderiza:
      - Resumen ejecutivo (cuántas en cada bucket).
      - Top 15 features como barras horizontales con magnitud relativa
        + dirección (↑/↓/≈) basada en SHAP value medio (signed).
      - Beeswarm PNG (distribución de SHAP por feature, color = signo).
      - Tabla colapsable con las 40 features clasificadas.
      - Lista explícita de features ruido / podables (accionable).

    Si `fi` es None, devuelve string vacío (la sección se omite del HTML).
    """
    if fi is None or fi.df.empty:
        return ""

    df = fi.df
    summary = fi.to_dict_summary()
    top_n = min(15, len(df))
    top_df = df.head(top_n)
    max_imp = float(top_df["importance_mean"].max()) or 1.0

    # ---- Bar chart (top 15) con dirección ----
    rows: list[str] = []
    for _, row in top_df.iterrows():
        imp = float(row["importance_mean"])
        std = float(row["importance_std"])
        direction = float(row.get("direction_mean", 0.0))
        arrow, dir_cls = _direction_arrow(direction)
        width_pct = max(2.0, min(100.0, abs(imp) / max_imp * 100.0))
        status = row["status"]
        label, icon = _FI_STATUS_LABELS.get(status, (status, ""))
        rows.append(
            f'<div class="fi-row fi-{escape(status)}">'
            f'<span class="fi-rank">#{int(row["rank"])}</span>'
            f'<span class="fi-name">{escape(str(row["feature"]))}</span>'
            f'<div class="fi-bar-wrap">'
            f'<div class="fi-bar" style="width: {width_pct:.1f}%"></div>'
            f'</div>'
            f'<span class="fi-val">{imp:.4f} <small>±{std:.4f}</small></span>'
            f'<span class="fi-dir {dir_cls}" title="impacto promedio: {direction:+.4f}">'
            f'{arrow}</span>'
            f'<span class="fi-badge {escape(status)}">{icon} {escape(label)}</span>'
            f'</div>'
        )
    bars_html = "".join(rows)

    # ---- Beeswarm PNG embebido ----
    if getattr(fi, "beeswarm_b64", None):
        beeswarm_html = (
            '<div class="fi-beeswarm">'
            '<img alt="Distribución SHAP por variable" '
            f'src="data:image/png;base64,{fi.beeswarm_b64}">'
            '<p class="fi-bee-cap">Cada punto es una predicción individual. '
            'Eje X = impacto de la variable en esa predicción (positivo = empuja '
            'arriba, negativo = empuja abajo). Color rojo/azul refuerza el signo. '
            'La <i>dispersión</i> de cada fila muestra qué tan variable es el '
            'efecto entre cosechas.</p>'
            '</div>'
        )
    else:
        beeswarm_html = ""

    # ---- Tabla colapsable: las 40 features ----
    table_rows: list[str] = []
    for _, row in df.iterrows():
        status = row["status"]
        label, icon = _FI_STATUS_LABELS.get(status, (status, ""))
        direction = float(row.get("direction_mean", 0.0))
        arrow, dir_cls = _direction_arrow(direction)
        table_rows.append(
            f'<tr class="fi-{escape(status)}">'
            f'<td>{int(row["rank"])}</td>'
            f'<td><code>{escape(str(row["feature"]))}</code></td>'
            f'<td>{float(row["importance_mean"]):.4f}</td>'
            f'<td>±{float(row["importance_std"]):.4f}</td>'
            f'<td class="fi-dir-cell {dir_cls}">{arrow} {direction:+.4f}</td>'
            f'<td><span class="fi-badge {escape(status)}">{icon} {escape(label)}</span></td>'
            f'</tr>'
        )
    table_html = "".join(table_rows)

    # ---- Lista accionable: ruido + podables ----
    actionable: list[str] = []
    for status in (STATUS_NOISE, STATUS_PRUNABLE):
        feats = df.loc[df["status"] == status, "feature"].tolist()
        if not feats:
            continue
        label, icon = _FI_STATUS_LABELS[status]
        desc = _FI_STATUS_DESCRIPTIONS[status]
        feat_chips = "".join(f'<code class="fi-chip">{escape(f)}</code>' for f in feats)
        actionable.append(
            f'<div class="fi-actionable {escape(status)}">'
            f'<div class="fi-act-title">{icon} {escape(label)} — {len(feats)} variable(s)</div>'
            f'<div class="fi-act-desc">{escape(desc)}</div>'
            f'<div class="fi-act-list">{feat_chips}</div>'
            f'</div>'
        )
    actionable_html = "".join(actionable) if actionable else (
        '<div class="fi-actionable info">'
        '<div class="fi-act-title">✅ Sin variables candidatas a eliminar</div>'
        '<div class="fi-act-desc">El modelo usa todas las features con importancia '
        'mayor que el umbral mínimo. La poda no aporta.</div>'
        '</div>'
    )

    # ---- Resumen ejecutivo ----
    n_total = summary["fi_n_features"]
    summary_html = (
        f'<div class="fi-summary">'
        f'<span class="fi-stat core">🟢 {summary["fi_n_core"]} núcleo</span> · '
        f'<span class="fi-stat util">🟡 {summary["fi_n_util"]} útiles</span> · '
        f'<span class="fi-stat podable">🔴 {summary["fi_n_prunable"]} podables</span> · '
        f'<span class="fi-stat ruido">⚫ {summary["fi_n_noise"]} ruido</span> '
        f'<small>(de {n_total} variables · método: {summary["fi_method"]} · '
        f'{summary["fi_n_models"]} modelos · MAE base = {summary["fi_mae_base"]:.4f})</small>'
        f'</div>'
    )

    return f"""
    <section>
      <div class="eyebrow">Importancia de variables (SHAP)</div>
      <h2>¿Qué variables sostienen el modelo?</h2>
      <p class="lead">Análisis post-hoc con SHAP TreeExplainer sobre los
      {summary["fi_n_models"]} modelos del ensemble. Para cada predicción se
      mide la contribución de cada variable. La <b>magnitud</b> indica cuánto
      pesa la variable en el modelo; la <b>dirección</b> (↑/↓) indica si en
      promedio empuja la predicción arriba o abajo. Variables con magnitud
      ≈ 0 son candidatas a eliminar.</p>

      {summary_html}

      <h3 style="margin-top:18px">Top {top_n} variables — magnitud y dirección</h3>
      <div class="fi-chart">{bars_html}</div>

      {beeswarm_html}

      <h3 style="margin-top:24px">Decisión de poda</h3>
      {actionable_html}

      <details class="fi-table-wrap" style="margin-top:18px">
        <summary>Ver clasificación completa de las {n_total} variables</summary>
        <table class="fi-table">
          <thead><tr>
            <th>Rank</th><th>Variable</th><th>Magnitud</th><th>Std</th>
            <th>Dirección</th><th>Estado</th>
          </tr></thead>
          <tbody>{table_html}</tbody>
        </table>
      </details>
    </section>
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

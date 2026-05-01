"""Seccion 'Detalle tecnico' (colapsable) del dashboard ejecutivo.

Para Data Science: justificacion del campeon, panel comparativo
Train/Test/Full por modelo, scatters Drift & Overfitting (OOF vs refit) y
boxplots del error por FORMATO/FUNDO con GLOBAL como referencia.
"""
from __future__ import annotations

from html import escape
from typing import List, Optional

import numpy as np
import pandas as pd

from src.step_05_evaluate.champion import ModelResult
from src.step_05_evaluate.diagnostics import plot_pred_vs_actual_plotly
from src.step_05_evaluate.html.helpers import fmt, kpi_card


def _kpi_panel_for_model(r: ModelResult, *, is_winner: bool, rank: int) -> str:
    mae_train = r.metrics.get("nested_cv_mae_train_mean")
    mae_test = r.metrics.get("nested_cv_mae_mean")
    r2_test = r.metrics.get("nested_cv_r2_mean")
    gap = r.metrics.get("nested_cv_gap_mean")
    full_h = r.full_metrics_h or {}
    business_oof = r.business_metrics_oof or {}
    business_full = r.full_metrics or {}

    badge_winner = (
        '<span class="badge winner-tag">CAMPEÓN</span>' if is_winner
        else '<span class="badge loser-tag">descartado</span>'
    )
    head = (
        f'<div class="head">'
        f'<div class="name">{escape(r.model_type.upper())} '
        f'<span style="color:#94a3b8;font-weight:500;font-size:12px">'
        f'· {r.elapsed_seconds:.1f}s</span></div>'
        f'<div><span class="badge rank">#{rank}</span>{badge_winner}</div>'
        f'</div>'
    )
    section_h = (
        '<div class="legend section-label">Unidad del modelo · KG/JR_H</div>'
        '<div class="kpi-row">'
        + kpi_card("Train (CV)", fmt(mae_train), "MAE", "train")
        + kpi_card("Test (CV)", fmt(mae_test), f"MAE · R²={fmt(r2_test)}")
        + kpi_card("Aplicación Total", fmt(full_h.get("mae")),
                   f"MAE · R²={fmt(full_h.get('r2'))}", "full")
        + '</div>'
    )
    section_b = (
        '<div class="legend section-label">Unidad de negocio · KG/JR (MAPE %)</div>'
        '<div class="kpi-row">'
        + kpi_card("Brecha Train→Test", fmt(gap, 4), "menor = más estable", "train")
        + kpi_card("Test (OOF)", fmt(business_oof.get("mape"), 2, "%"),
                   f"R²={fmt(business_oof.get('r2'))}")
        + kpi_card("Aplicación Total", fmt(business_full.get("mape"), 2, "%"),
                   f"R²={fmt(business_full.get('r2'))}", "full")
        + '</div>'
    )
    cls_card = "model-card winner" if is_winner else "model-card"
    return f'<div class="{cls_card}">{head}{section_h}{section_b}</div>'


def _scatter_block(y_real, y_pred, title: str, color: str) -> str:
    if y_real is None or y_pred is None or len(y_real) == 0:
        body = ('<div style="padding:32px;text-align:center;color:var(--gray-500);'
                'font-size:13px">No hay datos disponibles</div>')
    else:
        body = plot_pred_vs_actual_plotly(
            y_real, y_pred, title="",
            x_label="Real (KG/JR)", y_label="Predicción (KG/JR)", color=color,
        )
    return (
        f'<div class="chart-block">'
        f'<div class="chart-title">{escape(title)}</div>'
        f'{body}</div>'
    )


def _model_charts(r: ModelResult, *, is_winner: bool) -> str:
    bv = r.business_validation
    real_oof = getattr(bv, "kg_jr_real_oof", None) if bv else None
    pred_oof = getattr(bv, "kg_jr_pred_oof", None) if bv else None
    real_full = getattr(bv, "kg_jr_real_insample", None) if bv else None
    pred_full = getattr(bv, "kg_jr_pred_insample", None) if bv else None
    badge = ('<span class="badge winner-tag">CAMPEÓN</span>' if is_winner
             else '<span class="badge loser-tag">descartado</span>')
    return (
        f'<div class="model-block">'
        f'<h4>{escape(r.model_type.upper())} {badge}</h4>'
        f'<div class="charts-grid">'
        + _scatter_block(real_oof, pred_oof, "Test · OOF (honesto)", "#1f4e8a")
        + _scatter_block(real_full, pred_full, "Aplicación Total · refit (sesgado)", "#16a34a")
        + '</div></div>'
    )


def _build_boxplot(
    abs_err: np.ndarray, groups: pd.Series,
    title: str, group_label: str, min_n: int = 5,
) -> str:
    try:
        import plotly.graph_objects as go
    except ImportError:
        return ""
    if abs_err.size == 0:
        return ""

    fig = go.Figure()
    fig.add_trace(go.Box(
        y=abs_err, name="GLOBAL", boxmean="sd",
        marker=dict(color="#2a9d8f"), line=dict(color="#1f6e63"),
        boxpoints="outliers",
        hovertemplate=(f"Error: %{{y:.2f}} kg/jornal<br>n: {int(abs_err.size)}<extra>GLOBAL</extra>"),
    ))
    palette = ["#1f4e8a", "#b08968", "#d62828", "#6c757d", "#e9c46a", "#9d4edd",
               "#2d6a4f", "#bf812d"]
    cats = []
    for cat in groups.unique():
        if pd.isna(cat):
            continue
        mask = (groups == cat).to_numpy()
        if mask.sum() < min_n:
            continue
        cats.append((str(cat), abs_err[mask]))
    cats.sort(key=lambda kv: float(np.median(kv[1])), reverse=True)
    for i, (cat, err) in enumerate(cats):
        fig.add_trace(go.Box(
            y=err, name=cat, boxmean=True,
            marker=dict(color=palette[i % len(palette)]),
            boxpoints="outliers",
            hovertemplate=(
                f"Error: %{{y:.2f}} kg/jornal<br>{group_label}: {cat}<br>"
                f"n: {int(err.size)}<extra></extra>"
            ),
        ))

    p50 = float(np.percentile(abs_err, 50))
    p90 = float(np.percentile(abs_err, 90))
    fig.update_layout(
        title=dict(text=title, font=dict(size=14, color="#0c2a4d")),
        yaxis_title="Error absoluto (kg/jornal)", height=380,
        margin=dict(l=60, r=20, t=50, b=70),
        plot_bgcolor="white", paper_bgcolor="white",
        font=dict(family="-apple-system, Segoe UI, Roboto, sans-serif", size=11),
        showlegend=False,
        annotations=[dict(
            text=(
                f"<b>Lectura:</b> 50% del error ≤ {p50:.2f} · 90% ≤ {p90:.2f} kg/jornal · "
                "ordenados de peor (izq) a mejor (der)"
            ),
            xref="paper", yref="paper", x=0, y=-0.18,
            showarrow=False, align="left", font=dict(size=10.5, color="#555"),
        )],
    )
    fig.update_yaxes(gridcolor="#e7eaf0", zerolinecolor="#cbd5e0")
    try:
        return fig.to_html(include_plotlyjs=False, full_html=False,
                           config={"displaylogo": False, "responsive": True})
    except Exception:
        return ""


def _build_boxplot_block(
    X_aligned: Optional[pd.DataFrame], abs_errors: np.ndarray,
) -> str:
    if abs_errors.size == 0:
        return '<div class="legend">No hay datos OOF para boxplots.</div>'
    p50 = float(np.percentile(abs_errors, 50))
    p90 = float(np.percentile(abs_errors, 90))
    p99 = float(np.percentile(abs_errors, 99))
    stats = (
        '<div class="boxplot-stats">'
        f'<div class="stat-pill"><div class="label">Mediana (p50)</div>'
        f'<div class="val">{p50:.2f} <span style="font-size:11px;color:var(--gray-500)">kg/jr</span></div></div>'
        f'<div class="stat-pill"><div class="label">p90 (severos)</div>'
        f'<div class="val amber">{p90:.2f} <span style="font-size:11px;color:var(--gray-500)">kg/jr</span></div></div>'
        f'<div class="stat-pill"><div class="label">p99 (cola)</div>'
        f'<div class="val red">{p99:.2f} <span style="font-size:11px;color:var(--gray-500)">kg/jr</span></div></div>'
        '</div>'
    )
    cf = cu = ""
    if X_aligned is not None and len(X_aligned) == abs_errors.size:
        if "FORMATO" in X_aligned.columns:
            cf = _build_boxplot(abs_errors, X_aligned["FORMATO"].astype(str),
                                "Error por FORMATO (con GLOBAL como referencia)", "FORMATO")
        if "FUNDO" in X_aligned.columns:
            cu = _build_boxplot(abs_errors, X_aligned["FUNDO"].astype(str),
                                "Error por FUNDO (con GLOBAL como referencia)", "FUNDO")
    blocks = ""
    if cf:
        blocks += f'<div class="chart-block">{cf}</div>'
    if cu:
        blocks += f'<div class="chart-block">{cu}</div>'
    if not blocks:
        empty = pd.Series([""] * abs_errors.size)
        single = _build_boxplot(abs_errors, empty, "Distribución global del error", "GLOBAL")
        blocks = f'<div class="chart-block">{single}</div>'
    return f'{stats}<div class="charts-grid">{blocks}</div>'


def build_technical_section(
    *,
    results: List[ModelResult],
    champion: ModelResult,
    decision: dict,
    X_aligned: Optional[pd.DataFrame],
    abs_errors: np.ndarray,
) -> str:
    n_models = len(results)
    grid_cls = "models-grid"
    if n_models == 2:
        grid_cls += " cols-2"
    elif n_models >= 3:
        grid_cls += " cols-3"

    ranking_lookup = {row["model"]: row["rank"] for row in decision.get("ranking", [])}
    ordered = sorted(results, key=lambda r: ranking_lookup.get(r.model_type, 999))
    cards = "".join(
        _kpi_panel_for_model(r, is_winner=(r.model_type == champion.model_type),
                             rank=ranking_lookup.get(r.model_type, i + 1))
        for i, r in enumerate(ordered)
    )
    charts = "".join(
        _model_charts(r, is_winner=(r.model_type == champion.model_type))
        for r in ordered
    )
    boxplots = _build_boxplot_block(X_aligned, abs_errors)
    justification = decision.get("justification", "")
    criteria = " → ".join(decision.get("decision_criteria", []))

    return f"""
    <details class="technical">
      <summary>
        <span class="title">🔬 Detalle técnico (para el equipo de Data Science)</span>
        <span class="sub">Click para expandir · selección de campeón, métricas Train/Test/Full, gráficos de error</span>
      </summary>
      <div class="body">
        <div class="tech-block">
          <div class="eyebrow">Justificación técnica</div>
          <h3>¿Por qué ganó {escape(champion.model_type.upper())}?</h3>
          <p class="lead">Criterio aplicado (orden estricto): {escape(criteria)}</p>
          <div class="justify-text">{escape(justification)}</div>
        </div>

        <div class="tech-block">
          <div class="eyebrow">Panel comparativo · KPIs por modelo</div>
          <h3>Métricas Train · Test · Aplicación Total</h3>
          <p class="lead">Tarjetas por modelo en la unidad del modelo (KG/JR_H, nested CV) y la unidad de negocio (KG/JR, MAPE %).</p>
          <div class="{grid_cls}">{cards}</div>
        </div>

        <div class="tech-block">
          <div class="eyebrow">Monitorización · Drift &amp; Overfitting</div>
          <h3>Distribución predicción vs real (KG/JR)</h3>
          <p class="lead">Comparar Test (OOF, sin contaminar) con Aplicación Total (refit + predict all) revela overfitting si los puntos del segundo se pegan más a la diagonal que los del primero.</p>
          {charts}
        </div>

        <div class="tech-block">
          <div class="eyebrow">Errores por subgrupo · Campeón</div>
          <h3>¿Dónde se equivoca el modelo ganador?</h3>
          <p class="lead">Distribución del error absoluto (KG/JR, OOF) del campeón <b>{escape(champion.model_type.upper())}</b>. Compara cada FORMATO y FUNDO contra el GLOBAL: los grupos con mediana mayor son los más problemáticos.</p>
          {boxplots}
        </div>
      </div>
    </details>
    """

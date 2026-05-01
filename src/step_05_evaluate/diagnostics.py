"""Diagnosticos y graficos del pipeline final (Plotly).

Solo conserva el chart vivo: scatter Predicho vs Real (OOF / refit). El
resto de visualizaciones legacy (gauges, residuales, importancias, boxplots)
fue removido cuando el dashboard ejecutivo paso a renderizarlas inline en
`html/winner_dashboard.py`. Si vuelven a hacer falta, recuperar desde git
historico antes que reescribir.

El script plotly.js se carga UNA sola vez desde el <head> del HTML (ver
`html.styles._PLOTLY_JS_TAG`); por eso `_plotly_div` siempre pasa
`include_plotlyjs=False`.
"""
from __future__ import annotations

import numpy as np

# Paleta corporativa
_PRIMARY = "#1f4e8a"
_DANGER = "#d62828"

_PLOTLY_LAYOUT_DEFAULTS = dict(
    plot_bgcolor="white",
    paper_bgcolor="white",
    font=dict(family="-apple-system, Segoe UI, Roboto, sans-serif", size=11),
    margin=dict(l=60, r=20, t=50, b=50),
    hoverlabel=dict(bgcolor="white", font_size=12, bordercolor=_PRIMARY),
)


def _plotly_div(fig, div_id: str = "") -> str:
    """Devuelve <div> embebible (sin plotly.js) o "" si plotly no esta."""
    try:
        kwargs = {"include_plotlyjs": False, "full_html": False,
                  "config": {"displaylogo": False, "responsive": True}}
        if div_id:
            kwargs["div_id"] = div_id
        return fig.to_html(**kwargs)
    except Exception:
        return ""


def plot_pred_vs_actual_plotly(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    title: str = "Predicho vs Real (out-of-fold)",
    x_label: str = "Valor real",
    y_label: str = "Prediccion (OOF)",
    color: str = _PRIMARY,
) -> str:
    """Scatter interactivo con linea ideal y = x. Usa scattergl si n>5000."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        return ""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    if y_true.size == 0:
        return ""

    Trace = go.Scattergl if y_true.size > 5000 else go.Scatter
    lo = float(min(y_true.min(), y_pred.min()))
    hi = float(max(y_true.max(), y_pred.max()))

    fig = go.Figure()
    fig.add_trace(Trace(
        x=y_true, y=y_pred, mode="markers", name="observaciones",
        marker=dict(color=color, size=5, opacity=0.45,
                    line=dict(width=0)),
        hovertemplate=f"{x_label}: %{{x:.3f}}<br>{y_label}: %{{y:.3f}}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=[lo, hi], y=[lo, hi], mode="lines",
        name="y = x (ideal)",
        line=dict(color=_DANGER, dash="dash", width=1.5),
        hoverinfo="skip",
    ))
    fig.update_layout(
        title=dict(text=title, font=dict(size=14, color=_PRIMARY)),
        xaxis_title=x_label, yaxis_title=y_label,
        height=380, showlegend=True,
        legend=dict(font=dict(size=10), x=0.02, y=0.98, bgcolor="rgba(255,255,255,.7)"),
        **_PLOTLY_LAYOUT_DEFAULTS,
    )
    fig.update_xaxes(gridcolor="#e7eaf0", zerolinecolor="#cbd5e0")
    fig.update_yaxes(gridcolor="#e7eaf0", zerolinecolor="#cbd5e0")
    return _plotly_div(fig)

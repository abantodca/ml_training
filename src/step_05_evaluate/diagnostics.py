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


def plot_calibration_plotly(calibration_df) -> str:
    """Calibration plot para regresion: bin de pred vs media de real.

    Modelo perfectamente calibrado -> puntos sobre la diagonal y=x.
    Puntos arriba de la diagonal = modelo subestima en ese bin.
    Puntos abajo = sobreestima.
    """
    if calibration_df is None or len(calibration_df) == 0:
        return ""
    try:
        import plotly.graph_objects as go
    except ImportError:
        return ""

    bin_pred = calibration_df["bin_pred_mean"].to_numpy(dtype=float)
    bin_real = calibration_df["bin_real_mean"].to_numpy(dtype=float)
    counts = calibration_df["bin_count"].to_numpy(dtype=int)

    lo = float(min(bin_pred.min(), bin_real.min()))
    hi = float(max(bin_pred.max(), bin_real.max()))

    fig = go.Figure()
    # Diagonal ideal
    fig.add_trace(go.Scatter(
        x=[lo, hi], y=[lo, hi], mode="lines",
        name="Calibrado perfecto (y=x)",
        line=dict(color=_DANGER, dash="dash", width=1.5),
        hoverinfo="skip",
    ))
    # Puntos por bin (size proporcional a count)
    fig.add_trace(go.Scatter(
        x=bin_pred, y=bin_real, mode="markers+lines",
        name="Bins de prediccion (10)",
        marker=dict(color=_PRIMARY, size=8 + counts / counts.max() * 14,
                    opacity=0.75, line=dict(color="white", width=1)),
        line=dict(color=_PRIMARY, width=1, dash="dot"),
        hovertemplate=(
            "Predicho promedio: %{x:.2f}<br>"
            "Real promedio: %{y:.2f}<br>"
            "n filas: %{text}<extra></extra>"
        ),
        text=counts,
    ))
    fig.update_layout(
        title=dict(text="Calibracion: predicho vs real (bins)",
                   font=dict(size=14, color=_PRIMARY)),
        xaxis_title="Prediccion media (bin)",
        yaxis_title="Real media (bin)",
        height=350, showlegend=True,
        legend=dict(font=dict(size=10), x=0.02, y=0.98,
                    bgcolor="rgba(255,255,255,.7)"),
        **_PLOTLY_LAYOUT_DEFAULTS,
    )
    fig.update_xaxes(gridcolor="#e7eaf0", zerolinecolor="#cbd5e0")
    fig.update_yaxes(gridcolor="#e7eaf0", zerolinecolor="#cbd5e0")
    return _plotly_div(fig, div_id="calibration_plot")


def plot_partial_dependence_plotly(
    pipeline,
    X_sample,
    feature_names: list,
    top_k: int = 5,
) -> str:
    """Partial Dependence Plot (PDP) para las top-k features mas importantes.

    PDP estadistico (Friedman 2001): efecto marginal promedio de UNA feature
    sobre la prediccion, integrando el resto. Mas honesto que feature
    importance bruta (que solo dice cuanto se usa, no como afecta).

    Estrategia:
      1. Selecciona top-k features por feature_importances_ del modelo final
         (LGB/XGB exponen este atributo nativamente).
      2. Para cada una, sklearn.inspection.partial_dependence sobre 50 puntos
         del rango observado.
      3. Plotly con UN trace por feature (lineas).

    Tolera fallos: si pipeline no expone el modelo final o sklearn falla,
    devuelve string vacio (la seccion del dashboard se omite).
    """
    try:
        import plotly.graph_objects as go
        from sklearn.inspection import partial_dependence
    except ImportError:
        return ""

    # Resolver el modelo final dentro del wrapper (OOFEnsemble -> Pipeline -> TTR -> regressor)
    try:
        if hasattr(pipeline, "models_") and len(pipeline.models_) > 0:
            inner = pipeline.models_[0]  # primero del ensemble
        else:
            inner = pipeline
        # Top-k features por importance (LGB/XGB lo exponen)
        regressor = inner.named_steps.get("regressor") if hasattr(inner, "named_steps") else inner
        # TransformedTargetRegressor wrapping
        actual = getattr(regressor, "regressor_", regressor)
        importances = getattr(actual, "feature_importances_", None)
        if importances is None or len(importances) == 0:
            return ""
        top_idx = list(np.argsort(importances)[-top_k:][::-1])
    except Exception:
        return ""

    # X_sample debe estar PRE-PROCESADO (mismo space que vio el regressor).
    # Si X_sample es raw, sklearn.inspection.partial_dependence puede fallar.
    # Aqui asumimos que el caller ya pasa X transformado.
    fig = go.Figure()
    palette = ["#1f4e8a", "#2ca02c", "#ff7f0e", "#d62728", "#9467bd"]

    for plot_idx, feat_idx in enumerate(top_idx):
        try:
            pdp = partial_dependence(
                actual, X_sample, features=[feat_idx],
                kind="average", grid_resolution=30,
            )
            grid_x = pdp["grid_values"][0]
            avg_y = pdp["average"][0]
        except Exception:
            continue

        feat_label = (
            feature_names[feat_idx]
            if feat_idx < len(feature_names)
            else f"feature_{feat_idx}"
        )
        fig.add_trace(go.Scatter(
            x=grid_x, y=avg_y, mode="lines",
            name=feat_label,
            line=dict(color=palette[plot_idx % len(palette)], width=2),
            hovertemplate=f"{feat_label}: %{{x:.2f}}<br>Predicción: %{{y:.3f}}<extra></extra>",
        ))

    if len(fig.data) == 0:
        return ""

    fig.update_layout(
        title=dict(text=f"Top-{top_k} features: efecto marginal sobre la predicción (KG/JR_H)",
                   font=dict(size=14, color=_PRIMARY)),
        xaxis_title="Valor de la feature (rango observado)",
        yaxis_title="Predicción promedio",
        height=380, showlegend=True,
        legend=dict(font=dict(size=10), orientation="h",
                    yanchor="bottom", y=1.02, xanchor="right", x=1),
        **_PLOTLY_LAYOUT_DEFAULTS,
    )
    fig.update_xaxes(gridcolor="#e7eaf0", zerolinecolor="#cbd5e0")
    fig.update_yaxes(gridcolor="#e7eaf0", zerolinecolor="#cbd5e0")
    return _plotly_div(fig, div_id="pdp_plot")

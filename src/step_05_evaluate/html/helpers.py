"""Helpers de presentacion compartidos por las secciones del dashboard.

Cubren formateo numerico, tarjetas KPI generales y el boton de descarga.
Ningun helper aqui debe contener narrativa de negocio: solo wrapping HTML.
"""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Optional

import numpy as np


def fmt(value: Optional[float], digits: int = 4, suffix: str = "") -> str:
    """Formatea un float; devuelve '—' para None / NaN / Inf."""
    if value is None or (isinstance(value, float) and (np.isnan(value) or np.isinf(value))):
        return "—"
    return f"{value:.{digits}f}{suffix}"


def kpi_card(label: str, value: str, sub: str = "", flavor: str = "") -> str:
    """Tarjeta KPI generica usada en los paneles tecnicos."""
    cls = f"kpi {flavor}".strip()
    sub_html = f'<div class="sub">{escape(sub)}</div>' if sub else ""
    return (
        f'<div class="{cls}">'
        f'<div class="label">{escape(label)}</div>'
        f'<div class="value">{escape(value)}</div>'
        f'{sub_html}'
        f'</div>'
    )


def download_button(excel_path: Optional[str], variety: str) -> str:
    """Boton de descarga del Excel ejecutivo. Disabled si no hay archivo."""
    if not excel_path:
        return (
            '<a class="btn-download disabled">'
            '<span class="icon">⬇</span>'
            '<span><span class="label-main">Excel no disponible</span>'
            '<span class="label-sub">faltan columnas KG/JR · H-EF</span></span>'
            '</a>'
        )
    rel = Path(excel_path).name
    return (
        f'<a class="btn-download" href="{escape(rel)}" download>'
        f'<span class="icon">⬇</span>'
        f'<span><span class="label-main">Descargar Excel completo</span>'
        f'<span class="label-sub">{escape(rel)} · variedad {escape(variety)}</span></span>'
        f'</a>'
    )

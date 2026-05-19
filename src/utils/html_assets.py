"""Activos HTML compartidos entre capas (dashboard ejecutivo y diagnostics).

Centraliza la construccion del tag `<script>` de plotly.js para que tanto
el dashboard de `step_05_evaluate` como los reportes de `diagnostics`
consuman el mismo bundle sin acoplamientos cross-package.

`plotly_js_tag()` decide entre embeber plotly.js (offline, autocontenido)
o cargarlo desde CDN segun `REPORT_PLOTLY_OFFLINE`. La constante
`PLOTLY_JS_TAG` cachea el resultado para callers que solo necesitan el
string ya construido.
"""
from __future__ import annotations

from src.config import REPORT_PLOTLY_OFFLINE


def plotly_js_tag() -> str:
    """Tag <script> con plotly.js. Offline (default, ~4.5 MB embebido) o CDN.

    Modo controlado por `REPORT_PLOTLY_OFFLINE` en config:
      True  -> plotly.js inline (HTML autocontenido, funciona sin internet)
      False -> CDN (HTML mas liviano pero requiere internet)
    """
    cdn_tag = (
        '<script charset="utf-8" '
        'src="https://cdn.plot.ly/plotly-3.1.0.min.js"></script>'
    )
    if not REPORT_PLOTLY_OFFLINE:
        return cdn_tag
    try:
        from plotly.offline import get_plotlyjs
        return f'<script charset="utf-8">{get_plotlyjs()}</script>'
    except Exception:
        return cdn_tag


PLOTLY_JS_TAG = plotly_js_tag()


__all__ = ["plotly_js_tag", "PLOTLY_JS_TAG"]

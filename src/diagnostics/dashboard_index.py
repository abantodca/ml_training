"""Generador de `reports/index.html` — dashboard unificado de reportes (estatico).

Coexiste con la version JS-dinamica en `reports/index.html` (que reports/
ya tiene cuando arrancas con `task up`). Cuando usar cada uno:

    JS dinamico (siempre presente como `reports/index.html`):
      Pro:  auto-refresh al click en boton, descubre archivos sobre la marcha
      Contra: requiere nginx + autoindex (no funciona file://)
      Uso:  workflow de desarrollo, dashboards live durante training

    Python estatico (este modulo, escribe `reports/index_static.html`):
      Pro:  snapshot archivable, funciona file://, emailable, no depende de infra
      Contra: stale al agregar archivo nuevo (re-correr `task reports:dashboard`)
      Uso:  reports compartidos por email, archivos para auditoria, deploy S3 static

Para que NO se pisen, este modulo escribe a `index_static.html` (no
`index.html`). Asi ambos coexisten en `reports/`.



Escanea `reports/` y produce un HTML self-contained con:
    - Header (project + contador de reportes + timestamp de regeneracion)
    - Sidebar lateral (280px) con grupos auto-detectados:
        * EDA Diagnostico       (EDA_<variety>_<ts>.html)
        * Winner Dashboards     (Winner_<variety>.html)
        * Residual Diagnostics  (residuals_<variety>_<run>.html)
        * Excel Exports         (*.xlsx)
        * JSON / Metadata       (*.json)
        * Otros                  (todo lo demas)
    - Main content area con iframe que carga el HTML seleccionado al click.

Diferencia vs el index.html JS-based: este genera HTML ESTATICO (server-rendered).
Ventajas:
    - Funciona via file:// (no necesita nginx + autoindex)
    - Se puede emailar / archivar como snapshot del estado
    - Mas rapido de cargar (no hay fetch al directorio)
    - Compatible con S3 static website hosting en produccion

Uso:
    # Manual (host con Python disponible):
    python -m src.diagnostics.dashboard_index

    # Manual (dentro del container):
    docker compose run --rm --no-deps --entrypoint python trainer \\
      -m src.diagnostics.dashboard_index

    # Automatico: llamado por variety_runner al final de cada training,
    # asi siempre tenes un dashboard actualizado.
"""
from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Categorizacion de archivos por pattern
# ---------------------------------------------------------------------------
@dataclass
class ReportGroup:
    key: str
    label: str
    icon: str
    matcher: Callable[[str], bool]
    extractor: Callable[[str], "ReportLabel"]


@dataclass
class ReportLabel:
    name: str
    meta: str = ""


@dataclass
class ReportItem:
    filename: str
    name: str
    meta: str
    ext: str
    mtime: datetime


def _eda_label(filename: str) -> ReportLabel:
    """EDA_<variety>_<YYYY-MM-DD_HH-MM>.html → name=variety, meta=date"""
    base = filename.rsplit(".", 1)[0]
    m = re.match(r"^EDA_(.+?)_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})$", base)
    if m:
        return ReportLabel(name=m.group(1), meta=f"{m.group(2)} {m.group(3)}:{m.group(4)}")
    return ReportLabel(name=base.replace("EDA_", ""))


def _winner_label(filename: str) -> ReportLabel:
    base = filename.rsplit(".", 1)[0]
    return ReportLabel(name=base.replace("Winner_", ""))


def _residual_label(filename: str) -> ReportLabel:
    base = filename.rsplit(".", 1)[0]
    m = re.match(r"^residuals_(.+?)_(.+)$", base)
    if m:
        return ReportLabel(name=m.group(1), meta=m.group(2))
    return ReportLabel(name=base.replace("residuals_", ""))


def _generic_label(filename: str) -> ReportLabel:
    base = filename.rsplit(".", 1)[0]
    return ReportLabel(name=base)


GROUPS: List[ReportGroup] = [
    ReportGroup(
        key="eda", label="EDA Diagnostico", icon="📊",
        matcher=lambda f: f.startswith("EDA_") and f.endswith(".html"),
        extractor=_eda_label,
    ),
    ReportGroup(
        key="winner", label="Winner Dashboards", icon="🏆",
        matcher=lambda f: f.startswith("Winner_") and f.endswith(".html"),
        extractor=_winner_label,
    ),
    ReportGroup(
        key="resid", label="Residual Diagnostics", icon="🔬",
        matcher=lambda f: f.startswith("residuals_") and f.endswith(".html"),
        extractor=_residual_label,
    ),
    ReportGroup(
        key="excel", label="Excel Exports", icon="📑",
        matcher=lambda f: f.endswith(".xlsx"),
        extractor=_generic_label,
    ),
    ReportGroup(
        key="json", label="JSON / Metadata", icon="🗂️",
        matcher=lambda f: f.endswith(".json"),
        extractor=_generic_label,
    ),
    ReportGroup(
        key="other", label="Otros", icon="📄",
        matcher=lambda f: True,  # catch-all
        extractor=_generic_label,
    ),
]


def _classify(filename: str) -> Optional[ReportGroup]:
    for g in GROUPS:
        if g.matcher(filename):
            return g
    return None


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
def scan_reports(reports_dir: Path) -> dict[str, List[ReportItem]]:
    """Devuelve {group_key: [ReportItem, ...]} ordenado por mtime descendente."""
    if not reports_dir.exists():
        return {}

    grouped: dict[str, List[ReportItem]] = {}
    for path in reports_dir.iterdir():
        if not path.is_file():
            continue
        if path.name in ("index.html", "index_static.html"):
            continue  # no listamos los dashboards mismos
        if path.name.startswith("."):
            continue

        group = _classify(path.name)
        if group is None:
            continue
        ext = path.suffix.lstrip(".").lower()
        label = group.extractor(path.name)
        item = ReportItem(
            filename=path.name,
            name=label.name,
            meta=label.meta,
            ext=ext,
            mtime=datetime.fromtimestamp(path.stat().st_mtime),
        )
        grouped.setdefault(group.key, []).append(item)

    # Ordenar cada grupo: mas reciente primero
    for items in grouped.values():
        items.sort(key=lambda x: x.mtime, reverse=True)

    return grouped


# ---------------------------------------------------------------------------
# HTML render
# ---------------------------------------------------------------------------
# NOTE: el palette `:root` (--primary, --gray-*, etc) se duplica con
# `html_renderer.BASE_CSS`. Future-work: extraer a `src/diagnostics/_palette.py`
# como constantes Python f-string-able. Por ahora aceptamos duplicacion
# porque los layouts (sidebar/topbar vs cards/tables) son fundamentalmente
# distintos y compartir todo el CSS no aporta.
_CSS = """
:root {
  --primary: #2563eb;
  --primary-dark: #1d4ed8;
  --gray-50: #f8fafc;
  --gray-100: #f1f5f9;
  --gray-200: #e2e8f0;
  --gray-300: #cbd5e1;
  --gray-500: #64748b;
  --gray-700: #334155;
  --gray-900: #0f172a;
  --sidebar-width: 280px;
  --header-height: 56px;
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  color: var(--gray-900); background: var(--gray-50); overflow: hidden;
}
header.topbar {
  height: var(--header-height);
  background: linear-gradient(90deg, #1e3a8a, #2563eb);
  color: white; display: flex; align-items: center; justify-content: space-between;
  padding: 0 24px; box-shadow: 0 2px 8px rgba(15,23,42,.1);
  position: relative; z-index: 10;
}
header.topbar .brand { font-weight: 600; font-size: 16px; letter-spacing: -.01em; }
header.topbar .brand small { font-weight: 400; opacity: .75; margin-left: 8px; font-size: 12px; }
header.topbar .meta { font-size: 11px; opacity: .85; font-variant-numeric: tabular-nums; }
main.layout { display: flex; height: calc(100% - var(--header-height)); }
aside.sidebar {
  width: var(--sidebar-width); background: white;
  border-right: 1px solid var(--gray-200); overflow-y: auto; padding: 16px 0;
}
.group { margin-bottom: 12px; }
.group-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 16px; font-size: 10px; font-weight: 600;
  color: var(--gray-500); text-transform: uppercase; letter-spacing: .08em;
}
.group-header .icon { font-size: 13px; }
.group-header .badge {
  background: var(--gray-100); color: var(--gray-700);
  padding: 1px 7px; border-radius: 999px; font-weight: 600; font-size: 10px;
}
.item {
  display: flex; align-items: center; gap: 8px;
  padding: 9px 16px 9px 32px;
  color: var(--gray-700); cursor: pointer; font-size: 13px;
  border-left: 2px solid transparent;
  transition: background .1s, border-color .1s, color .1s;
}
.item:hover { background: var(--gray-50); color: var(--gray-900); }
.item.active {
  background: #eff6ff; border-left-color: var(--primary);
  color: var(--primary-dark); font-weight: 500;
}
.item .item-body { display: flex; flex-direction: column; min-width: 0; flex: 1; }
.item .item-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.item .meta { font-size: 10px; color: var(--gray-500); margin-top: 1px; }
.item.active .meta { color: var(--primary); opacity: .7; }
.ext {
  font-size: 9px; padding: 1px 5px; border-radius: 3px;
  font-weight: 600; letter-spacing: .05em;
}
.ext.html { background: #dbeafe; color: #1e40af; }
.ext.xlsx { background: #dcfce7; color: #166534; }
.ext.json { background: #fef3c7; color: #92400e; }
.ext.other { background: var(--gray-100); color: var(--gray-700); }
.empty {
  padding: 32px 16px; text-align: center; color: var(--gray-500); font-size: 13px;
}
section.content { flex: 1; position: relative; background: var(--gray-100); }
section.content iframe {
  width: 100%; height: calc(100% - 33px); border: 0; background: white;
  display: block; margin-top: 33px;
}
.placeholder {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  height: 100%; color: var(--gray-500); padding: 32px; text-align: center;
}
.placeholder .big { font-size: 48px; margin-bottom: 16px; opacity: .35; }
.placeholder h2 { color: var(--gray-700); margin: 0 0 8px; font-size: 18px; }
.breadcrumb {
  position: absolute; top: 0; left: 0; right: 0;
  background: white; border-bottom: 1px solid var(--gray-200);
  padding: 8px 16px; font-size: 12px; color: var(--gray-700);
  display: flex; align-items: center; justify-content: space-between;
  box-shadow: 0 1px 2px rgba(15,23,42,.04); z-index: 5;
}
.breadcrumb .path { font-family: 'JetBrains Mono', Menlo, monospace; font-size: 11px; }
.breadcrumb .path-sep { color: var(--gray-300); margin: 0 6px; }
.breadcrumb a { color: var(--primary); text-decoration: none; font-size: 11px; }
.breadcrumb a:hover { text-decoration: underline; }
@media (max-width: 760px) {
  :root { --sidebar-width: 100%; }
  main.layout { flex-direction: column; }
  aside.sidebar { max-height: 200px; border-right: 0; border-bottom: 1px solid var(--gray-200); }
}
"""


def _format_meta(item: ReportItem) -> str:
    if item.meta:
        return item.meta
    return item.mtime.strftime("%Y-%m-%d %H:%M")


def _ext_class(ext: str) -> str:
    return ext if ext in {"html", "xlsx", "json"} else "other"


def render_dashboard(grouped: dict[str, List[ReportItem]]) -> str:
    """Renderiza el HTML completo del dashboard."""
    total = sum(len(v) for v in grouped.values())
    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Pre-seleccionar: el mas reciente Winner > EDA > primer item
    initial_href = ""
    for key in ("winner", "eda", "resid", "excel", "json", "other"):
        items = grouped.get(key, [])
        if items:
            initial_href = items[0].filename
            break

    sidebar_html = ""
    if total == 0:
        sidebar_html = (
            '<div class="empty">No hay reportes aun.<br><br>'
            'Corre <code>task train VARIETIES=POP TUNING=smoke</code> para generar.'
            '</div>'
        )
    else:
        for g in GROUPS:
            items = grouped.get(g.key, [])
            if not items:
                continue
            sidebar_html += f'<div class="group">'
            sidebar_html += (
                f'<div class="group-header">'
                f'<span><span class="icon">{g.icon}</span> {escape(g.label)}</span>'
                f'<span class="badge">{len(items)}</span>'
                f'</div>'
            )
            for it in items:
                ext_cls = _ext_class(it.ext)
                meta_line = (
                    f'<div class="meta">{escape(_format_meta(it))}</div>'
                    if (it.meta or it.mtime) else ""
                )
                sidebar_html += (
                    f'<div class="item" data-href="{escape(it.filename)}" '
                    f'onclick="loadReport(this)">'
                    f'<span class="ext {ext_cls}">{escape(it.ext.upper())}</span>'
                    f'<div class="item-body">'
                    f'<div class="item-name">{escape(it.name)}</div>'
                    f'{meta_line}'
                    f'</div></div>'
                )
            sidebar_html += '</div>'

    js = """
    function loadReport(el) {
      const href = el.dataset.href;
      if (!href) return;
      document.querySelectorAll('.item.active').forEach(x => x.classList.remove('active'));
      el.classList.add('active');
      const content = document.getElementById('content');
      const ext = (href.match(/\\.([^.]+)$/) || [])[1] || '';
      if (ext.toLowerCase() === 'html') {
        content.innerHTML = `
          <div class="breadcrumb">
            <span class="path">reports<span class="path-sep">/</span>${decodeURIComponent(href)}</span>
            <a href="${encodeURI(href)}" target="_blank">Abrir en pestana nueva &#x2197;</a>
          </div>
          <iframe src="${encodeURI(href)}" referrerpolicy="no-referrer"></iframe>
        `;
      } else {
        content.innerHTML = `
          <div class="placeholder">
            <div class="big">${ext.toLowerCase() === 'xlsx' ? '\\uD83D\\uDCD1' : '\\uD83D\\uDDC2\\uFE0F'}</div>
            <h2>${decodeURIComponent(href)}</h2>
            <p>${ext.toUpperCase()} no se embebe en el navegador.</p>
            <p style="margin-top:16px;">
              <a href="${encodeURI(href)}" download
                 style="color:var(--primary); text-decoration:none; font-weight:500;">
                &#x2B07; Descargar archivo
              </a>
            </p>
          </div>`;
      }
    }
    // Auto-load inicial
    const initial = document.querySelector('.item[data-href="__INITIAL_HREF__"]');
    if (initial) loadReport(initial);
    """.replace("__INITIAL_HREF__", initial_href)

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ml_training - Reports Dashboard</title>
<style>{_CSS}</style>
</head>
<body>
<header class="topbar">
  <div class="brand">ml_training <small>- Reports Dashboard</small></div>
  <div class="meta">{total} reporte{'' if total == 1 else 's'} - generado {escape(ts_now)}</div>
</header>
<main class="layout">
  <aside class="sidebar">{sidebar_html}</aside>
  <section class="content" id="content">
    <div class="placeholder">
      <div class="big">&#x1F4CA;</div>
      <h2>Selecciona un reporte</h2>
      <p>El sidebar lista todos los reportes disponibles en <code>reports/</code>, agrupados por tipo.</p>
    </div>
  </section>
</main>
<script>{js}</script>
</body>
</html>"""


def write_dashboard(reports_dir: Path,
                    *, filename: str = "index_static.html") -> Path:
    """Escanea reports_dir y escribe reports_dir/<filename>. Devuelve el path.

    Default `filename='index_static.html'` para NO pisar el `index.html`
    JS-dinamico que vive en reports/. Si alguien quiere reemplazarlo,
    pasar `filename='index.html'` explicito.
    """
    grouped = scan_reports(reports_dir)
    html = render_dashboard(grouped)
    out = reports_dir / filename
    out.write_text(html, encoding="utf-8")
    total = sum(len(v) for v in grouped.values())
    logger.info(f"Dashboard regenerado: {out} ({total} reportes indexados)")
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main() -> int:
    parser = argparse.ArgumentParser(description="Genera reports/index.html")
    parser.add_argument(
        "--reports-dir", default=None,
        help="Override del directorio reports/ (default: config.REPORTS_DIR)",
    )
    args = parser.parse_args()

    if args.reports_dir:
        reports_dir = Path(args.reports_dir)
    else:
        from src.config import REPORTS_DIR  # lazy import
        reports_dir = REPORTS_DIR

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = write_dashboard(reports_dir)
    print(f"\n  Dashboard: file://{out}")
    print(f"  Via nginx: http://localhost:8080/reports/index.html\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

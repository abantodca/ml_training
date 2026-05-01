"""Recursos compartidos del dashboard HTML: CSS + plotly.js bundle.

`_PLOTLY_JS_TAG` decide entre embeber plotly.js (offline, autocontenido)
o cargarlo desde CDN segun `REPORT_PLOTLY_OFFLINE`. `DASHBOARD_CSS` es
la hoja de estilos completa del dashboard ejecutivo (hero, secciones,
KPIs, charts, technical details).
"""
from __future__ import annotations

from src.config import REPORT_PLOTLY_OFFLINE


def _build_plotly_js_tag() -> str:
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


_PLOTLY_JS_TAG = _build_plotly_js_tag()


DASHBOARD_CSS = """
:root {
  --navy:#0c2a4d; --navy-2:#1e3a5f; --gold:#c9a961;
  --green:#16a34a; --green-2:#22c55e; --amber:#f59e0b; --red:#dc2626;
  --gray-50:#f8fafc; --gray-100:#f1f5f9; --gray-200:#e2e8f0;
  --gray-500:#64748b; --gray-700:#334155; --gray-900:#0f172a;
}
* { box-sizing: border-box; }
body {
  margin:0; font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
  background: var(--gray-50); color: var(--gray-900); line-height:1.6;
}
.wrap { max-width: 1280px; margin: 0 auto; padding: 24px; }

/* ===== HERO ===== */
.hero {
  border-radius: 16px; padding: 32px 36px; margin-bottom: 22px;
  box-shadow: 0 6px 24px rgba(12,42,77,.18);
  display: flex; flex-wrap: wrap; gap: 22px; align-items: stretch;
}
.hero.green   { background: linear-gradient(135deg, #0c2a4d 0%, #16a34a 140%); color: white; }
.hero.green-2 { background: linear-gradient(135deg, #0c2a4d 0%, #1e3a5f 100%); color: white; }
.hero.amber   { background: linear-gradient(135deg, #92400e 0%, #f59e0b 130%); color: white; }
.hero.red     { background: linear-gradient(135deg, #7f1d1d 0%, #dc2626 130%); color: white; }
.hero-text { flex: 1 1 540px; }
.hero .eyebrow { color: rgba(255,255,255,.7); font-size: 12px;
  letter-spacing: .14em; text-transform: uppercase; font-weight: 600; }
.hero h1 { margin: 8px 0 4px; font-size: 30px; font-weight: 700; }
.hero .meta { color: rgba(255,255,255,.78); font-size: 13px; }

.verdict-badge {
  display: inline-flex; align-items: center; gap: 10px;
  background: rgba(255,255,255,.18); backdrop-filter: blur(2px);
  padding: 10px 18px; border-radius: 10px; font-size: 14px;
  font-weight: 700; margin-top: 14px; letter-spacing: .03em;
  border: 1.5px solid rgba(255,255,255,.32);
}
.verdict-badge .icon { font-size: 22px; }
.verdict-headline { font-size: 17px; margin: 14px 0 6px; font-weight: 600; }
.verdict-body { font-size: 14px; color: rgba(255,255,255,.92);
  max-width: 720px; line-height: 1.55; }

.hero-side { flex: 0 0 auto; min-width: 240px; display: flex;
  flex-direction: column; gap: 10px; justify-content: center; }
.btn-download {
  display: inline-flex; align-items: center; gap: 12px;
  background: white; color: var(--navy); font-weight: 700;
  padding: 14px 20px; border-radius: 10px; font-size: 14px;
  text-decoration: none; box-shadow: 0 4px 14px rgba(0,0,0,.18);
  transition: transform .12s ease;
}
.btn-download:hover { transform: translateY(-1px); }
.btn-download.disabled { background: rgba(255,255,255,.14);
  color: rgba(255,255,255,.7); cursor: not-allowed; box-shadow: none; }
.btn-download .icon { font-size: 18px; }
.btn-download .label-sub { display:block; font-size: 11px; font-weight: 500;
  opacity: .85; margin-top: 2px; }

/* ===== SECTIONS ===== */
section { background:white; border:1px solid var(--gray-200); border-radius:14px;
  padding:24px 28px; margin-bottom:18px; box-shadow:0 1px 2px rgba(0,0,0,.03); }
section h2 { margin:0 0 4px; font-size:19px; color: var(--navy); }
section .eyebrow { color: var(--gray-500); font-size: 11px;
  letter-spacing: .14em; text-transform: uppercase; font-weight: 600; }
section .lead { color: var(--gray-700); font-size: 14px; margin: 4px 0 18px; }

/* ===== CONTEXT CARDS ===== */
.ctx-grid {
  display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}
.ctx-card { background: var(--gray-50); border: 1px solid var(--gray-200);
  border-radius: 10px; padding: 12px 14px; }
.ctx-card .label { font-size: 11px; color: var(--gray-500);
  text-transform: uppercase; letter-spacing: .1em; font-weight: 700; }
.ctx-card .value { font-size: 22px; font-weight: 700; color: var(--navy);
  margin-top: 4px; }
.ctx-card .sub { font-size: 12px; color: var(--gray-500); margin-top: 4px; }

/* ===== MEGA KPIs ===== */
.kpi-mega-grid {
  display: grid; gap: 16px; grid-template-columns: 1fr; margin-top: 10px;
}
@media (min-width: 980px) { .kpi-mega-grid { grid-template-columns: repeat(3, 1fr); } }
.kpi-mega {
  border: 1px solid var(--gray-200); border-radius: 12px; padding: 20px;
  background: linear-gradient(180deg, #ffffff 0%, var(--gray-50) 100%);
  display: flex; flex-direction: column; gap: 8px;
}
.kpi-mega .question { font-size: 13px; color: var(--gray-500);
  text-transform: uppercase; letter-spacing: .08em; font-weight: 700;
  display: flex; align-items: center; gap: 8px; }
.kpi-mega .score-pill { font-size: 10px; padding: 2px 10px; border-radius: 999px;
  font-weight: 700; letter-spacing: .04em; }
.score-pill.ALTO  { background: #dcfce7; color: #166534; }
.score-pill.MEDIO { background: #fef3c7; color: #92400e; }
.score-pill.BAJO  { background: #fee2e2; color: #991b1b; }
.kpi-mega .headline { font-size: 22px; font-weight: 700; color: var(--navy);
  line-height: 1.25; margin: 4px 0 6px; }
.kpi-mega .detail { font-size: 13px; color: var(--gray-700); line-height: 1.55; }
.kpi-mega .technical { font-size: 11px; color: var(--gray-500);
  font-family: "SF Mono", Menlo, Consolas, monospace;
  border-top: 1px dashed var(--gray-200); padding-top: 8px; margin-top: 6px; }

/* ===== ACTIONS ===== */
.action {
  display: flex; gap: 14px; padding: 14px 16px; border-radius: 10px;
  margin-bottom: 10px; align-items: flex-start;
}
.action.critical { background: #fef2f2; border-left: 4px solid var(--red); }
.action.warning  { background: #fffbeb; border-left: 4px solid var(--amber); }
.action.info     { background: #f0fdf4; border-left: 4px solid var(--green); }
.action .icon { font-size: 22px; line-height: 1; flex: 0 0 auto; }
.action .body-wrap { flex: 1 1 auto; }
.action .title { font-weight: 700; color: var(--navy); margin-bottom: 4px;
  font-size: 14px; }
.action .body { font-size: 13px; color: var(--gray-700); line-height: 1.55; }

/* ===== HELP / GLOSSARY ===== */
details.help {
  background: var(--gray-50); border: 1px solid var(--gray-200);
  border-radius: 12px; padding: 14px 18px; margin-bottom: 18px;
}
details.help[open] { background: white; }
details.help summary {
  cursor: pointer; font-weight: 700; color: var(--navy); font-size: 14px;
  list-style: none; user-select: none; padding: 4px 0;
  display: flex; align-items: center; gap: 10px;
}
details.help summary::-webkit-details-marker { display: none; }
details.help summary::before {
  content: "▸"; font-size: 14px; color: var(--gray-500);
  transition: transform .15s ease;
}
details.help[open] summary::before { transform: rotate(90deg); }
details.help .body { padding-top: 10px; color: var(--gray-700); font-size: 14px; }
.glossary-grid {
  display: grid; gap: 8px; grid-template-columns: 1fr; margin-top: 10px;
}
@media (min-width: 800px) { .glossary-grid { grid-template-columns: 1fr 1fr; } }
.gloss-row {
  background: white; border: 1px solid var(--gray-200); border-radius: 8px;
  padding: 10px 12px;
}
.gloss-row .term { font-weight: 700; color: var(--navy); font-size: 13px;
  margin-bottom: 3px; }
.gloss-row .def { font-size: 12.5px; color: var(--gray-700); line-height: 1.5; }

/* ===== TECHNICAL DETAILS (collapsible wrapper) ===== */
details.technical {
  background: white; border: 1px solid var(--gray-200); border-radius: 14px;
  padding: 0; margin-bottom: 18px; overflow: hidden;
}
details.technical summary {
  cursor: pointer; padding: 18px 24px; background: var(--gray-50);
  border-bottom: 1px solid var(--gray-200); list-style: none;
  user-select: none; display: flex; align-items: center; gap: 12px;
}
details.technical summary::-webkit-details-marker { display: none; }
details.technical summary::before {
  content: "▸"; color: var(--gray-500); transition: transform .15s ease;
}
details.technical[open] summary::before { transform: rotate(90deg); }
details.technical summary .title { font-weight: 700; color: var(--navy);
  font-size: 15px; }
details.technical summary .sub { color: var(--gray-500); font-size: 12px;
  margin-left: auto; }
details.technical .body { padding: 22px 26px; }

.tech-block { margin-bottom: 28px; }
.tech-block:last-child { margin-bottom: 0; }
.tech-block h3 { margin: 0 0 4px; font-size: 16px; color: var(--navy); }
.tech-block .lead { color: var(--gray-700); font-size: 13.5px;
  margin: 4px 0 14px; }
.tech-block .eyebrow { color: var(--gray-500); font-size: 11px;
  letter-spacing: .14em; text-transform: uppercase; font-weight: 600; }

/* ===== JUSTIFY ===== */
.justify-text { background: var(--gray-100); border-left: 4px solid var(--gold);
  padding: 14px 18px; border-radius: 8px; font-size: 14px; color: var(--gray-700); }

/* ===== MODELS GRID + KPI CARDS (technical, reused) ===== */
.models-grid { display: grid; gap: 16px; grid-template-columns: 1fr; }
@media (min-width: 900px) { .models-grid.cols-2 { grid-template-columns: 1fr 1fr; } }
@media (min-width: 1100px) { .models-grid.cols-3 { grid-template-columns: repeat(3, 1fr); } }

.model-card {
  border:1px solid var(--gray-200); border-radius: 10px; padding: 16px;
  background: white;
}
.model-card.winner { border: 2px solid var(--gold); background: #fffdf6; }
.model-card .head { display:flex; align-items:center; justify-content:space-between;
  margin-bottom: 10px; }
.model-card .name { font-weight: 700; font-size: 15px; color: var(--navy); }
.model-card .badge {
  font-size: 10.5px; padding: 3px 10px; border-radius: 999px; font-weight: 700;
  letter-spacing: .04em; margin-left: 4px;
}
.badge.winner-tag { background: var(--gold); color: var(--navy); }
.badge.loser-tag  { background: var(--gray-200); color: var(--gray-700); }
.badge.rank { background: var(--navy); color: white; }

.kpi-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top:6px; }
.kpi {
  background: var(--gray-50); border-radius: 8px; padding: 8px 10px;
  border: 1px solid var(--gray-200);
}
.kpi .label { color: var(--gray-500); font-size: 10px; text-transform: uppercase;
  letter-spacing: .1em; font-weight: 700; }
.kpi .value { font-size: 17px; font-weight: 700; color: var(--navy); margin-top: 2px; }
.kpi .sub { color: var(--gray-500); font-size: 11px; margin-top: 2px; }
.kpi.train .value { color: var(--gray-700); }
.kpi.full  .value { color: var(--green); }

.legend { font-size: 12px; color: var(--gray-500); margin-top: 10px; line-height: 1.6; }
.legend.section-label { font-weight: 700; color: var(--gray-700); margin-top: 12px; }

/* ===== CHARTS ===== */
.charts-grid { display: grid; gap: 14px; grid-template-columns: 1fr; margin-top: 6px; }
@media (min-width: 1000px) { .charts-grid { grid-template-columns: 1fr 1fr; } }
.chart-block { border: 1px solid var(--gray-200); border-radius: 10px;
  padding: 6px; background: white; }
.chart-block .chart-title { font-size: 11.5px; color: var(--gray-500);
  text-transform: uppercase; letter-spacing: .1em; padding: 6px 8px 0; font-weight: 700; }

.model-block { margin-bottom: 16px; }
.model-block h4 { margin: 4px 0 6px; color: var(--navy); font-size: 14px;
  display: flex; align-items: center; gap: 10px; }

.boxplot-stats { display: grid; gap: 8px; grid-template-columns: repeat(3, 1fr);
  margin-bottom: 12px; }
.stat-pill { background: var(--gray-50); border: 1px solid var(--gray-200);
  border-radius: 8px; padding: 8px 12px; text-align: center; }
.stat-pill .label { font-size: 10px; color: var(--gray-500); text-transform: uppercase;
  letter-spacing: .1em; font-weight: 700; }
.stat-pill .val { font-size: 18px; font-weight: 700; color: var(--navy); }
.stat-pill .val.amber { color: var(--amber); }
.stat-pill .val.red { color: var(--red); }

footer { text-align:center; color: var(--gray-500); font-size: 12px;
  padding: 16px 0; margin-top: 8px; }
"""


__all__ = ["_PLOTLY_JS_TAG", "DASHBOARD_CSS"]

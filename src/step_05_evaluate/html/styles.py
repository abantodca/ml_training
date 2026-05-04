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

/* ===== STACKING PILL (hero) ===== */
.stacking-pill {
  display: inline-flex; align-items: center; gap: 12px;
  margin-top: 14px; padding: 8px 14px; border-radius: 999px;
  background: rgba(255,255,255,.14);
  border: 1.2px solid rgba(255,255,255,.28);
  font-size: 12.5px; line-height: 1.3;
}
.stacking-pill .dot {
  width: 9px; height: 9px; border-radius: 50%;
  box-shadow: 0 0 0 3px rgba(255,255,255,.18);
  flex: 0 0 auto;
}
.stacking-pill.active .dot { background: #22c55e; }
.stacking-pill.neutral .dot { background: #fbbf24; }
.stacking-pill.fallback .dot { background: #cbd5e0; }
.stacking-pill .text { display: flex; flex-direction: column; }
.stacking-pill .lbl { font-weight: 700; letter-spacing: .04em; }
.stacking-pill .sub { color: rgba(255,255,255,.78); font-size: 11.5px; }
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

.kpi-footnote {
  font-size: 12.5px; color: var(--gray-700); margin-top: 14px;
  padding: 10px 14px; background: var(--gray-50);
  border-left: 3px solid var(--gold); border-radius: 6px; line-height: 1.55;
}

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

/* ===== STACKING / META BLOCK (technical) ===== */
.stacking-banner {
  display: flex; gap: 12px; align-items: flex-start;
  padding: 14px 16px; border-radius: 10px; margin: 4px 0 14px;
  font-size: 13.5px; line-height: 1.55; border: 1px solid;
}
.stacking-banner.good {
  background: #f0fdf4; border-color: #bbf7d0; color: #166534;
}
.stacking-banner.neutral {
  background: #fffbeb; border-color: #fde68a; color: #92400e;
}
.stacking-banner.fallback {
  background: var(--gray-50); border-color: var(--gray-200);
  color: var(--gray-700);
}
.stacking-banner .banner-icon { font-size: 18px; line-height: 1.2; flex: 0 0 auto; }
.stacking-banner .banner-text { flex: 1 1 auto; font-weight: 500; }

.meta-kpi-grid {
  display: grid; gap: 10px; margin: 10px 0 16px;
  grid-template-columns: repeat(2, 1fr);
}
@media (min-width: 900px) {
  .meta-kpi-grid { grid-template-columns: repeat(4, 1fr); }
}
.meta-kpi {
  border: 1px solid var(--gray-200); border-radius: 10px;
  padding: 12px 14px; background: white;
}
.meta-kpi .label {
  font-size: 10.5px; color: var(--gray-500); text-transform: uppercase;
  letter-spacing: .1em; font-weight: 700;
}
.meta-kpi .value {
  font-size: 22px; font-weight: 700; color: var(--navy); margin-top: 4px;
  font-variant-numeric: tabular-nums;
}
.meta-kpi .sub { font-size: 11.5px; color: var(--gray-500); margin-top: 2px; }
.meta-kpi.good .value { color: #166534; }
.meta-kpi.warn .value { color: var(--amber); }
.meta-kpi.neutral .value { color: var(--gray-700); }

.meta-features {
  border: 1px solid var(--gray-200); border-radius: 10px;
  padding: 12px 14px; margin: 4px 0 14px; background: white;
}
.meta-features-title {
  font-size: 12.5px; font-weight: 700; color: var(--navy);
  display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px;
  border-bottom: 1px solid var(--gray-100); padding-bottom: 6px;
}
.meta-features-title .hint {
  font-size: 11px; color: var(--gray-500); font-weight: 500;
  letter-spacing: .04em;
}
.meta-features-table {
  width: 100%; border-collapse: collapse; font-size: 12.5px;
}
.meta-features-table td {
  padding: 6px 8px; border-bottom: 1px solid var(--gray-100);
}
.meta-features-table td:first-child {
  font-family: "SF Mono", Menlo, Consolas, monospace;
  color: var(--navy); white-space: nowrap;
}
.meta-features-table td code {
  background: var(--gray-100); padding: 1px 6px; border-radius: 4px;
  font-size: 12px;
}
.meta-features-table tbody tr:last-child td { border-bottom: 0; }

.ftype-pill {
  display: inline-block; padding: 2px 9px; border-radius: 999px;
  font-size: 11px; font-weight: 600; letter-spacing: .02em;
}
.ftype-pill.ftype-pred { background: #fef3c7; color: #92400e; }
.ftype-pill.ftype-cat { background: #dbeafe; color: #1e40af; }
.ftype-pill.ftype-flag { background: #fde2e2; color: #991b1b; }
.ftype-pill.ftype-cont { background: #dcfce7; color: #166534; }

.meta-tuning {
  border: 1px solid var(--gray-200); border-radius: 10px;
  padding: 12px 14px; margin: 4px 0 14px; background: var(--gray-50);
}
.meta-tuning-title {
  font-size: 12.5px; font-weight: 700; color: var(--navy);
  margin-bottom: 8px;
}
.meta-tuning-grid {
  display: grid; gap: 8px;
  grid-template-columns: repeat(2, 1fr);
}
@media (min-width: 720px) {
  .meta-tuning-grid { grid-template-columns: repeat(3, 1fr); }
}
@media (min-width: 1080px) {
  .meta-tuning-grid { grid-template-columns: repeat(6, 1fr); }
}
.meta-tuning-grid > div {
  background: white; border: 1px solid var(--gray-200);
  border-radius: 8px; padding: 8px 10px;
  display: flex; flex-direction: column; gap: 2px;
}
.meta-tuning-grid .k {
  font-size: 10.5px; color: var(--gray-500); text-transform: uppercase;
  letter-spacing: .1em; font-weight: 700;
}
.meta-tuning-grid .v {
  font-size: 15px; font-weight: 700; color: var(--navy);
  font-variant-numeric: tabular-nums;
}

.meta-tech-line {
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: 11.5px; color: var(--gray-500);
  border-top: 1px dashed var(--gray-200); padding-top: 8px;
  margin-top: 8px;
}

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

.boxplot-stats { display: grid; gap: 8px;
  grid-template-columns: repeat(2, 1fr); margin-bottom: 14px; }
@media (min-width: 720px) {
  .boxplot-stats { grid-template-columns: repeat(3, 1fr); }
}
@media (min-width: 1080px) {
  .boxplot-stats { grid-template-columns: repeat(6, 1fr); }
}
.stat-pill { background: var(--gray-50); border: 1px solid var(--gray-200);
  border-radius: 10px; padding: 10px 12px; text-align: center; }
.stat-pill .label { font-size: 10px; color: var(--gray-500); text-transform: uppercase;
  letter-spacing: .1em; font-weight: 700; }
.stat-pill .val { font-size: 18px; font-weight: 700; color: var(--navy);
  margin-top: 2px; }
.stat-pill .val.amber { color: var(--amber); }
.stat-pill .val.red { color: var(--red); }
.stat-pill .unit { font-size: 11px; color: var(--gray-500); font-weight: 500; }

/* ===== SUBGROUP TABS (errors by group) ===== */
.subgrp-tabs { background: white; border: 1px solid var(--gray-200);
  border-radius: 12px; overflow: hidden; }
.subgrp-tabs-nav {
  display: flex; flex-wrap: wrap; gap: 0;
  background: var(--gray-50); border-bottom: 1px solid var(--gray-200);
  padding: 0 4px;
}
.subgrp-tab-btn {
  appearance: none; background: transparent; border: 0;
  padding: 12px 16px; cursor: pointer;
  font-family: inherit; font-size: 12.5px; font-weight: 600;
  color: var(--gray-500); letter-spacing: .04em;
  border-bottom: 2.5px solid transparent;
  transition: color .12s ease, border-color .12s ease, background .12s ease;
}
.subgrp-tab-btn:hover { color: var(--navy); background: rgba(12,42,77,.04); }
.subgrp-tab-btn.active {
  color: var(--navy); border-bottom-color: var(--gold);
  background: white;
}
.subgrp-tabs-body { padding: 16px; background: white; }
.subgrp-panel { display: none; }
.subgrp-panel.active { display: block; }

.subgrp-grid { display: grid; gap: 16px; grid-template-columns: 1fr; }
@media (min-width: 1080px) {
  .subgrp-grid { grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); }
}
.subgrp-chart { border: 1px solid var(--gray-200); border-radius: 10px;
  padding: 6px; background: white; min-width: 0; }
.subgrp-table-wrap { border: 1px solid var(--gray-200); border-radius: 10px;
  background: white; padding: 10px 12px; overflow-x: auto; min-width: 0; }
.subgrp-table-title { font-size: 12px; color: var(--gray-700);
  font-weight: 700; padding: 4px 4px 10px; border-bottom: 1px solid var(--gray-100);
  display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
.subgrp-table-hint { font-size: 10.5px; color: var(--gray-500); font-weight: 500;
  letter-spacing: .02em; margin-left: auto; }

.subgrp-table { width: 100%; border-collapse: collapse;
  font-size: 12.5px; font-variant-numeric: tabular-nums; }
.subgrp-table thead th {
  text-align: left; padding: 8px 10px;
  font-size: 10.5px; color: var(--gray-500); font-weight: 700;
  letter-spacing: .08em; text-transform: uppercase;
  border-bottom: 1px solid var(--gray-200); position: sticky; top: 0;
  background: white;
}
.subgrp-table thead th.num { text-align: right; }
.subgrp-table tbody td { padding: 8px 10px; border-bottom: 1px solid var(--gray-100); }
.subgrp-table tbody td.num { text-align: right; color: var(--gray-700); }
.subgrp-table tbody tr:last-child td { border-bottom: 0; }
.subgrp-table tbody tr.subgrp-row:hover { background: var(--gray-50); }
.subgrp-table tbody tr.subgrp-row.global {
  background: #fffdf6; font-weight: 600;
}
.subgrp-table tbody tr.subgrp-row.global td { color: var(--navy); }
.grp-name { display: inline-block; max-width: 220px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom;
  color: var(--navy); font-weight: 600; }

.ratio-pill { display: inline-block; min-width: 52px; padding: 2px 9px;
  border-radius: 999px; font-size: 11px; font-weight: 700;
  letter-spacing: .02em; text-align: center; }
.ratio-pill.ref { background: var(--gray-100); color: var(--gray-700); }
.ratio-pill.good { background: #dcfce7; color: #166534; }
.ratio-pill.neutral { background: var(--gray-100); color: var(--gray-700); }
.ratio-pill.warn { background: #fef3c7; color: #92400e; }
.ratio-pill.bad { background: #fee2e2; color: #991b1b; }

footer { text-align:center; color: var(--gray-500); font-size: 12px;
  padding: 16px 0; margin-top: 8px; }
"""


# CSS aislado de la seccion de feature importance. Vive aparte de
# DASHBOARD_CSS para que el modo retroactivo (parche en HTML existente)
# pueda inyectar SOLO estos estilos sin reescribir todo el bloque <style>.
FI_DASHBOARD_CSS = """
/* ===== Feature Importance ===== */
.fi-summary { display:flex; flex-wrap:wrap; gap:14px; align-items:baseline;
  padding:12px 16px; background:var(--gray-50); border-radius:8px;
  border:1px solid var(--gray-200); font-size:14px; }
.fi-summary small { color:var(--gray-600); }
.fi-stat { font-weight:600; }
.fi-stat.core    { color:#166534; }
.fi-stat.util    { color:#92400e; }
.fi-stat.podable { color:#991b1b; }
.fi-stat.ruido   { color:#1f2937; }

.fi-chart { display:grid; gap:6px; margin-top:10px; }
.fi-row { display:grid; grid-template-columns: 36px 1fr 26% auto 28px auto;
  align-items:center; gap:10px; padding:8px 12px; border-radius:6px;
  background:var(--gray-50); border:1px solid var(--gray-200); font-size:13px; }
.fi-row .fi-rank { font-weight:700; color:var(--gray-500); font-size:12px; }
.fi-row .fi-name { font-family:ui-monospace,Menlo,monospace; font-size:13px; }
.fi-row .fi-bar-wrap { background:var(--gray-200); height:14px; border-radius:7px;
  overflow:hidden; }
.fi-row .fi-bar { height:100%; background:#3b82f6; border-radius:7px; transition:width .3s; }
.fi-row.fi-core    .fi-bar { background:#22c55e; }
.fi-row.fi-util    .fi-bar { background:#3b82f6; }
.fi-row.fi-podable .fi-bar { background:#f59e0b; }
.fi-row.fi-ruido   .fi-bar { background:#ef4444; }
.fi-row .fi-val { font-family:ui-monospace,Menlo,monospace; font-size:12px;
  color:var(--gray-700); white-space:nowrap; }
.fi-row .fi-val small { color:var(--gray-500); font-size:11px; }
.fi-badge { display:inline-block; padding:2px 8px; border-radius:999px;
  font-size:11px; font-weight:700; white-space:nowrap; }
.fi-badge.core    { background:#dcfce7; color:#166534; }
.fi-badge.util    { background:#dbeafe; color:#1e40af; }
.fi-badge.podable { background:#fee2e2; color:#991b1b; }
.fi-badge.ruido   { background:#1f2937; color:#f3f4f6; }

.fi-actionable { padding:12px 16px; margin-top:10px; border-radius:8px;
  border-left:4px solid var(--gray-400); background:var(--gray-50); }
.fi-actionable.ruido   { border-left-color:#ef4444; background:#fef2f2; }
.fi-actionable.podable { border-left-color:#f59e0b; background:#fffbeb; }
.fi-actionable.info    { border-left-color:#22c55e; background:#f0fdf4; }
.fi-actionable .fi-act-title { font-weight:700; font-size:14px; margin-bottom:4px; }
.fi-actionable .fi-act-desc  { font-size:13px; color:var(--gray-700); margin-bottom:8px; }
.fi-actionable .fi-act-list  { display:flex; flex-wrap:wrap; gap:6px; }
.fi-chip { font-family:ui-monospace,Menlo,monospace; font-size:12px;
  padding:2px 8px; background:var(--gray-100); border-radius:4px;
  border:1px solid var(--gray-300); }

.fi-dir { display:inline-flex; align-items:center; justify-content:center;
  width:24px; height:24px; border-radius:50%; font-weight:700; font-size:14px;
  cursor:help; }
.fi-dir.up      { color:#166534; background:#dcfce7; }
.fi-dir.down    { color:#991b1b; background:#fee2e2; }
.fi-dir.neutral { color:var(--gray-700); background:var(--gray-100); }
.fi-dir-cell.up      { color:#166534; }
.fi-dir-cell.down    { color:#991b1b; }
.fi-dir-cell.neutral { color:var(--gray-700); }

.fi-beeswarm { margin-top:18px; padding:14px; background:var(--gray-50);
  border:1px solid var(--gray-200); border-radius:8px; }
.fi-beeswarm img { max-width:100%; height:auto; display:block; margin:0 auto; }
.fi-bee-cap { font-size:12px; color:var(--gray-600); margin-top:8px;
  line-height:1.5; }

.fi-table-wrap summary { cursor:pointer; padding:8px 12px;
  background:var(--gray-50); border-radius:6px; font-weight:600; }
.fi-table { width:100%; border-collapse:collapse; margin-top:10px; font-size:13px; }
.fi-table th, .fi-table td { padding:6px 10px; text-align:left;
  border-bottom:1px solid var(--gray-200); }
.fi-table th { background:var(--gray-100); font-weight:700; }
.fi-table tr:hover { background:var(--gray-50); }
.fi-table code { font-size:12px; }
"""


# DASHBOARD_CSS completo = base + FI. El renderer de training inyecta
# DASHBOARD_CSS en <style>; el modo retro inyecta solo FI_DASHBOARD_CSS
# como parche, asi no duplica reglas existentes.
DASHBOARD_CSS = DASHBOARD_CSS + FI_DASHBOARD_CSS


__all__ = ["_PLOTLY_JS_TAG", "DASHBOARD_CSS", "FI_DASHBOARD_CSS"]

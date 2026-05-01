"""Comparador de runs de entrenamiento (lee logs/business_audit.jsonl).

Muestra una tabla por consola con la trayectoria de las metricas en KG/JR
(unidad de negocio) entre runs sucesivos. Util para detectar:

  - Regresiones entre entrenamientos.
  - Si XGB vs LGB esta ayudando.
  - Si cambiar el tuning profile (smoke -> dev -> prod) realmente mueve el R2.
  - Tendencia general (mejorando o empeorando).

Uso:
    python scripts/audit_compare.py                       # todos los runs
    python scripts/audit_compare.py --variety POP         # filtra variedad
    python scripts/audit_compare.py --last 5              # solo los ultimos 5
    python scripts/audit_compare.py --variety POP --last 10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "business_audit.jsonl"


def _load_records(path: Path) -> list[dict]:
    if not path.exists():
        print(f"No existe {path}. Corre al menos 1 entrenamiento primero.", file=sys.stderr)
        return []
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"WARN: linea {i} corrupta, ignorada: {e}", file=sys.stderr)
    return records


def _safe(d: dict, *path, default=float("nan")):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur if cur is not None else default


def _fmt(v, spec):
    try:
        return format(float(v), spec)
    except (TypeError, ValueError):
        return "-"


def _delta(curr, prev, spec="+.4f"):
    try:
        d = float(curr) - float(prev)
        if d == 0:
            return "  ="
        return format(d, spec)
    except (TypeError, ValueError):
        return "  -"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--variety", default=None, help="Filtra por variedad (ej. POP)")
    p.add_argument("--model", default=None, help="Filtra por modelo (xgb|lgb)")
    p.add_argument("--last", type=int, default=None, help="Solo mostrar los ultimos N runs")
    p.add_argument("--file", default=str(LOG_FILE), help="Ruta al JSONL (default: logs/business_audit.jsonl)")
    args = p.parse_args(argv)

    records = _load_records(Path(args.file))
    if not records:
        return 1

    # Filtros
    if args.variety:
        records = [r for r in records if r.get("variety") == args.variety]
    if args.model:
        records = [r for r in records if r.get("model_type") == args.model]

    # Orden cronologico
    records.sort(key=lambda r: r.get("timestamp", ""))
    if args.last:
        records = records[-args.last:]

    if not records:
        print("Sin runs que coincidan con los filtros.", file=sys.stderr)
        return 1

    # ---- Header ----
    title = "AUDITORIA DE RUNS - metricas en KG/JR (unidad de negocio)"
    if args.variety:
        title += f"  |  variety={args.variety}"
    if args.model:
        title += f"  |  model={args.model}"
    print()
    print(title)
    print("=" * len(title))
    print(
        f"{'TIMESTAMP':17} {'VAR':6} {'MOD':4} {'TUNE':6} "
        f"{'R2_KGH':>7} {'R2_KGJR':>8} {'MAE_KGJR':>9} {'MAPE%':>7} "
        f"{'dR2':>8} {'dMAE':>8} {'dMAPE%':>8}  RUN_ID"
    )
    print("-" * 130)

    prev: dict | None = None
    for r in records:
        ts = (r.get("timestamp") or "")[:16]
        variety = r.get("variety", "")
        model = r.get("model_type", "")
        # Compat: logs viejos guardaban 'profile'; los nuevos guardan 'tuning'.
        tuning = r.get("tuning") or r.get("profile", "")
        run_id = (r.get("mlflow_run_id") or "")[:8]

        r2_kgh = _safe(r, "metrics_kg_jr_h_oof", "r2")
        r2_kgjr = _safe(r, "metrics_kg_jr_oof", "r2")
        mae_kgjr = _safe(r, "metrics_kg_jr_oof", "mae")
        mape_kgjr = _safe(r, "metrics_kg_jr_oof", "mape")

        # Deltas vs run anterior (mismo filtro)
        if prev is not None:
            d_r2 = _delta(r2_kgjr, _safe(prev, "metrics_kg_jr_oof", "r2"), "+.4f")
            d_mae = _delta(mae_kgjr, _safe(prev, "metrics_kg_jr_oof", "mae"), "+.3f")
            d_mape = _delta(mape_kgjr, _safe(prev, "metrics_kg_jr_oof", "mape"), "+.2f")
        else:
            d_r2 = d_mae = d_mape = "  -"

        print(
            f"{ts:17} {variety:6} {model:4} {tuning:6} "
            f"{_fmt(r2_kgh, '.4f'):>7} {_fmt(r2_kgjr, '.4f'):>8} "
            f"{_fmt(mae_kgjr, '.3f'):>9} {_fmt(mape_kgjr, '.2f'):>7} "
            f"{d_r2:>8} {d_mae:>8} {d_mape:>8}  {run_id}"
        )
        prev = r

    # ---- Best run summary ----
    print("-" * 130)
    best = max(records, key=lambda r: _safe(r, "metrics_kg_jr_oof", "r2", default=-9e9))
    worst = min(records, key=lambda r: _safe(r, "metrics_kg_jr_oof", "r2", default=9e9))
    best_tune = best.get("tuning") or best.get("profile", "")
    worst_tune = worst.get("tuning") or worst.get("profile", "")
    print(
        f"MEJOR  R2: {_fmt(_safe(best, 'metrics_kg_jr_oof', 'r2'), '.4f')} "
        f"({best.get('variety')} {best.get('model_type')} {best_tune} "
        f"@ {(best.get('timestamp') or '')[:16]} run={best.get('mlflow_run_id', '')[:8]})"
    )
    print(
        f"PEOR   R2: {_fmt(_safe(worst, 'metrics_kg_jr_oof', 'r2'), '.4f')} "
        f"({worst.get('variety')} {worst.get('model_type')} {worst_tune} "
        f"@ {(worst.get('timestamp') or '')[:16]} run={worst.get('mlflow_run_id', '')[:8]})"
    )
    print(f"TOTAL runs comparados: {len(records)}")
    print()
    print("Leyenda:")
    print("  R2_KGH    = R2 en KG/JR_H (lo que el modelo predice directamente)")
    print("  R2_KGJR   = R2 en KG/JR (la unidad de negocio: KG/JR_H * H-EF)  <- gerencial")
    print("  MAE/MAPE  = error en kg por jornal (KG/JR), OOF (honesto)")
    print("  dR2/dMAE  = delta vs el run inmediatamente anterior de la tabla")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

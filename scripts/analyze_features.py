"""Permutation importance del campeon ya entrenado.

Permite identificar features con importancia ~0 (candidatas a podar) SIN
re-entrenar. Carga el ultimo registered model (rnd-forest-<variety>) desde
mlruns/, corre permutation_importance contra el dataset completo, y emite
ranking + CSV.

Uso basico:
    python scripts/analyze_features.py --variety POP
    python scripts/analyze_features.py --variety POP --top 30 --n-repeats 5
    python scripts/analyze_features.py --variety POP --sample 3000

Modo retroactivo (sin re-entrenar, actualiza el run existente del champion):
    python scripts/analyze_features.py --variety POP --log-to-mlflow
    python scripts/analyze_features.py --variety POP --log-to-mlflow --rerender-html

  --log-to-mlflow : sube el CSV como artifact al run del champion + tags resumen.
  --rerender-html : regenera reports/Winner_<variety>.html con la nueva seccion.

Salida:
    reports/feature_importance_<variety>.csv  (rank, feature, importance_mean,
                                               importance_std, status)
    Stdout: tabla top-N + lista de candidatos a podar.

Notas:
- Usa src.step_05_evaluate.feature_importance.compute_feature_importance
  (la misma funcion que invoca el training pipeline en `variety_runner.py`),
  asi el CSV/tags/HTML quedan IDENTICOS entre training y modo retroactivo.
- Permuta features RAW (las columnas que el cliente del modelo envia).
- scoring='neg_mean_absolute_error' es el default (la metrica del modelo).
- Tiempo estimado: ~3-8 min con 10k filas y ~40 features.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

# Asegura que `src/` sea importable cuando el script se ejecuta directo.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mlflow
import mlflow.sklearn
from mlflow.tracking import MlflowClient

from src.config import MLFLOW_TRACKING_URI, MODEL_REGISTRY_PREFIX, REPORTS_DIR
from src.step_01_load.data_loader import load_data
from src.step_05_evaluate.feature_importance import (
    FeatureImportanceResult,
    compute_feature_importance,
)


def find_latest_champion(variety: str) -> tuple[object, str, str]:
    """Carga la ultima version registrada del campeon. Devuelve (pipeline, run_id, version)."""
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    client = MlflowClient()
    model_name = f"{MODEL_REGISTRY_PREFIX}{variety}".strip("_")
    versions = client.search_model_versions(f"name='{model_name}'")
    if not versions:
        raise RuntimeError(
            f"No hay versiones registradas para '{model_name}'. "
            f"Corre 'task train VARIETIES={variety}' primero."
        )
    latest = max(versions, key=lambda v: int(v.version))
    pipeline = mlflow.sklearn.load_model(f"models:/{model_name}/{latest.version}")
    return pipeline, latest.run_id, latest.version


import re as _re

# Columnas legacy que el data_loader ya no produce pero modelos antiguos
# siguen esperando como input. Inyectarlas como 0.0 = "no cold-start", que
# es el valor del 99% de las filas en el dataset original.
_LEGACY_COLD_FLAGS = ["LAG_FF_COLD", "LAG_FF_SEASONAL_COLD"]


def _patch_dataset_for_legacy_columns(
    pipeline, X: pd.DataFrame
) -> tuple[pd.DataFrame, list[str]]:
    """Agrega columnas que el modelo guardado espera pero el data_loader actual ya no produce.

    Estrategia: intenta `pipeline.predict(X.head(1))` para detectar las
    columnas faltantes via KeyError. Si falla, agrega las columnas de
    `_LEGACY_COLD_FLAGS` como zeros y reintenta. Si pasa, devuelve X igual.
    Devuelve (X_patched, missing_cols_added).
    """
    # Sanity: si predict pasa, no hay nada que hacer.
    try:
        _ = pipeline.predict(X.head(1))
        return X, []
    except KeyError as exc:
        msg = str(exc)
    except Exception:
        # Otro error (no de columnas faltantes) -> dejamos que pete arriba.
        return X, []

    # Parsea nombres faltantes de la KeyError (formato: "['col1', 'col2'] not in index").
    matches = _re.findall(r"'([^']+)'", msg)
    missing = [c for c in matches if c not in X.columns]
    if not missing:
        # Fallback: si la regex no matchea, intentamos las cold flags conocidas.
        missing = [c for c in _LEGACY_COLD_FLAGS if c not in X.columns]
        if not missing:
            return X, []

    X_out = X.copy()
    for col in missing:
        X_out[col] = 0.0

    # Verifica que ahora si funcione.
    try:
        _ = pipeline.predict(X_out.head(1))
        return X_out, missing
    except Exception:
        return X_out, missing  # el caller manejara si sigue rompiendo


def log_to_mlflow(fi: FeatureImportanceResult, run_id: str, csv_path: Path) -> None:
    """Sube el CSV + tags resumen al run existente del champion."""
    client = MlflowClient()
    client.log_artifact(run_id=run_id, local_path=str(csv_path))
    for k, v in fi.to_dict_summary().items():
        if isinstance(v, (int, float)):
            client.log_metric(run_id=run_id, key=k, value=float(v))
        else:
            client.set_tag(run_id=run_id, key=k, value=str(v))


# Marcadores que envuelven la seccion FI inyectada. Permiten reemplazo
# idempotente: si la seccion ya estaba (de un retro anterior), la sustituimos
# en lugar de duplicarla.
_FI_START = "<!-- FI_SHAP_SECTION_START -->"
_FI_END = "<!-- FI_SHAP_SECTION_END -->"
_FI_CSS_START = "<!-- FI_SHAP_CSS_START -->"
_FI_CSS_END = "<!-- FI_SHAP_CSS_END -->"


def rerender_html(
    variety: str,
    run_id: str,
    fi: FeatureImportanceResult,
    pipeline=None,
) -> Path:
    """Patcha el HTML existente del champion con la seccion SHAP.

    En lugar de regenerar el HTML desde cero (lo que perderia la seccion
    Detalle Tecnico que requiere business_validation y oof_y_* que NO viven
    en los tags de MLflow), abrimos `reports/Winner_<variety>.html`,
    inyectamos:
      - el CSS de SHAP en <head> (entre marcadores _FI_CSS_*)
      - la seccion HTML antes de <footer> (entre marcadores _FI_*)

    Si los marcadores ya existen (de un retro anterior), reemplazamos el
    contenido entre ellos -> idempotente. Si el HTML no existe, fallback a
    regenerarlo desde cero (quedara incompleto pero al menos tendra FI).
    """
    from src.config import REPORTS_DIR
    from src.step_05_evaluate.html.sections import build_feature_importance_section
    from src.step_05_evaluate.html.styles import FI_DASHBOARD_CSS

    html_path = REPORTS_DIR / f"Winner_{variety}.html"
    fi_section = build_feature_importance_section(fi)
    if not fi_section:
        raise RuntimeError("La seccion SHAP esta vacia; nada que parchar")

    # Si el HTML ya existe, parche in-place (preserva todo lo demas).
    if html_path.exists():
        html = html_path.read_text(encoding="utf-8")

        # 1) Inyecta o reemplaza CSS de SHAP.
        css_block = f"{_FI_CSS_START}<style>{FI_DASHBOARD_CSS}</style>{_FI_CSS_END}"
        if _FI_CSS_START in html and _FI_CSS_END in html:
            # Reemplaza bloque previo.
            pre, _, rest = html.partition(_FI_CSS_START)
            _, _, post = rest.partition(_FI_CSS_END)
            html = pre + css_block + post
        else:
            # Inyecta antes de </head>.
            html = html.replace("</head>", css_block + "</head>", 1)

        # 2) Inyecta o reemplaza la seccion FI.
        section_block = f"{_FI_START}{fi_section}{_FI_END}"
        if _FI_START in html and _FI_END in html:
            pre, _, rest = html.partition(_FI_START)
            _, _, post = rest.partition(_FI_END)
            html = pre + section_block + post
        else:
            # Inyecta antes del <footer>.
            html = html.replace("<footer", section_block + "<footer", 1)

        html_path.write_text(html, encoding="utf-8")
        return html_path

    # Fallback: HTML no existe, hay que generar desde cero (incompleto).
    return _full_render_fallback(variety, run_id, fi, pipeline)


def _full_render_fallback(
    variety: str,
    run_id: str,
    fi: FeatureImportanceResult,
    pipeline,
) -> Path:
    """Genera Winner_<variety>.html desde cero cuando no existe.

    Limitacion: sin business_validation ni oof_y_* el detalle tecnico queda
    incompleto. Solo se invoca si el HTML original se borro / nunca existio.
    """
    from src.config import REPORTS_DIR
    from src.step_05_evaluate.champion import ModelResult
    from src.step_05_evaluate.html.winner_dashboard import render_winner_dashboard

    client = MlflowClient()
    run = client.get_run(run_id)
    metrics = {k: float(v) for k, v in run.data.metrics.items()}
    params = {k: v for k, v in run.data.params.items()}
    tags = {k: v for k, v in run.data.tags.items()}

    champion = ModelResult(
        model_type=tags.get("model_type", "xgb"),
        metrics=metrics,
        best_params=params,
        mlflow_run_id=run_id,
        pipeline_path=tags.get("pipeline_path", ""),
        elapsed_seconds=float(metrics.get("elapsed_seconds", 0.0)),
        full_metrics={
            "mape": float(metrics.get("full_mape", float("nan"))),
            "mae": float(metrics.get("full_mae", float("nan"))),
            "r2": float(metrics.get("full_r2", float("nan"))),
        },
        business_metrics_oof={
            "mape": float(metrics.get("business_oof_mape", float("nan"))),
            "mae": float(metrics.get("business_oof_mae", float("nan"))),
            "r2": float(metrics.get("business_oof_r2", float("nan"))),
        },
    )
    X, _y = load_data(sheet=variety)
    if pipeline is not None:
        X, _ = _patch_dataset_for_legacy_columns(pipeline, X)
    excel_candidate = REPORTS_DIR / f"Winner_{variety}.xlsx"
    excel_path = str(excel_candidate) if excel_candidate.exists() else None
    return render_winner_dashboard(
        variety=variety,
        results=[champion],
        champion=champion,
        decision={"justification": "Retroactivo (modo --rerender-html, fallback)"},
        excel_path=excel_path,
        X_raw=X,
        feature_importance=fi,
    )


def _print_human_summary(fi: FeatureImportanceResult, top_n: int) -> None:
    """Imprime ranking top-N + listas accionables para terminal."""
    df = fi.df
    summary = fi.to_dict_summary()
    print(
        f"\nResumen | core={summary['fi_n_core']} util={summary['fi_n_util']} "
        f"podable={summary['fi_n_prunable']} ruido={summary['fi_n_noise']} "
        f"(de {summary['fi_n_features']} features · MAE base={summary['fi_mae_base']:.4f})"
    )
    print(f"\nTop {top_n} features por magnitud SHAP (mean |SHAP|):")
    cols = ["rank", "feature", "importance_mean", "share", "direction_mean", "status"]
    print(df[cols].head(top_n).to_string(index=False))

    podables = fi.prunable_features
    ruido = fi.noise_features
    if podables:
        print(f"\nCandidatos a podar (share <1% del importance total): {podables}")
    if ruido:
        print(
            f"\nFeatures con contribucion DESPRECIABLE (share <0.1%): "
            f"el modelo casi no las usa, eliminarlas no degrada."
        )
        print(df[df["feature"].isin(ruido)][cols].to_string(index=False))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--variety", required=True, help="Variedad (hoja del Excel)")
    p.add_argument("--top", type=int, default=25, help="Cuantas features mostrar")
    p.add_argument("--n-repeats", type=int, default=10, help="Repeticiones por feature")
    p.add_argument(
        "--scoring",
        default="neg_mean_absolute_error",
        choices=["neg_mean_absolute_error", "r2", "neg_root_mean_squared_error"],
    )
    p.add_argument(
        "--sample",
        type=int,
        default=0,
        help="Si >0, submuestrea N filas para acelerar",
    )
    p.add_argument(
        "--log-to-mlflow",
        action="store_true",
        help="Sube CSV + tags al run del champion (modo retroactivo)",
    )
    p.add_argument(
        "--rerender-html",
        action="store_true",
        help="Regenera Winner_<variety>.html con la nueva seccion",
    )
    args = p.parse_args()

    print(f"[1/4] Cargando champion para variedad '{args.variety}'...")
    pipeline, run_id, version = find_latest_champion(args.variety)
    print(f"      Run: {run_id}")
    print(f"      Version: v{version}")

    print(f"[2/4] Cargando datos...")
    X, y = load_data(sheet=args.variety)
    print(f"      X.shape = {X.shape}, y.shape = {y.shape}")

    # Compatibilidad: si el pipeline guardado espera columnas que el data_loader
    # actual ya no produce (ej. cold flags eliminadas tras la poda), inyectamos
    # zeros para que predict() no rompa. Las nuevas columnas no aportaran a SHAP
    # (zero-variance) y eso es exactamente lo que queremos verificar.
    X, added = _patch_dataset_for_legacy_columns(pipeline, X)
    if added:
        print(f"      Compat shim: agregadas como zeros las cols {added}")

    if args.sample and args.sample < len(X):
        rng = np.random.default_rng(42)
        idx = rng.choice(len(X), size=args.sample, replace=False)
        X = X.iloc[idx].reset_index(drop=True)
        y = y.iloc[idx].reset_index(drop=True)
        print(f"      Submuestreado a {len(X)} filas")

    print(f"[3/4] SHAP TreeExplainer | sample={2000} | sobre K modelos del ensemble")
    print(f"      (esto tarda ~30s-2min; SHAP exacto sobre arboles)")
    fi = compute_feature_importance(
        pipeline, X, y,
        n_repeats=args.n_repeats,
        scoring=args.scoring,
    )

    print(f"[4/4] Resultados")
    _print_human_summary(fi, top_n=args.top)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = REPORTS_DIR / f"feature_importance_{args.variety}.csv"
    fi.df.to_csv(out_csv, index=False)
    print(f"\nCSV guardado: {out_csv}")

    if args.log_to_mlflow:
        print(f"\n[+] Subiendo a MLflow run {run_id}...")
        try:
            log_to_mlflow(fi, run_id=run_id, csv_path=out_csv)
            print(f"    OK: artifact + tags subidos al run del champion")
        except Exception as exc:
            print(f"    ERROR al subir a MLflow: {exc}")

    if args.rerender_html:
        print(f"\n[+] Regenerando HTML del champion con la nueva seccion...")
        try:
            html_path = rerender_html(args.variety, run_id=run_id, fi=fi, pipeline=pipeline)
            print(f"    OK: {html_path}")
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"    ERROR al regenerar HTML: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

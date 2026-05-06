"""Phase 0 GAMM: validacion standalone del corrector de residuos.

Antes de invertir 5h en implementar GAMMResidualCorrector + ChampionPlusGAMM
+ integracion al pipeline, este script responde UNA pregunta concreta:

    ¿Hay senal aprovechable en los residuos del champion?

Si la respuesta es VERDE (Δ MAE ≤ -1%), procedemos a Phase 1. Si es ROJO o
GRIS, abortamos GAMM y volvemos a feature engineering tradicional. Costo de
no hacerlo: ~5h perdidas en codigo que termina con auto-fallback permanente.

Logica
------
1. Carga el champion del run actual (artifacts/final_pipeline_*.joblib)
   y los OOF arrays persistidos por single_run.py (artifacts/oof_*.npz).
2. Computa residuos en log-space (consistente con TransformedTargetRegressor
   del champion: y_true_log - y_pred_log).
3. Construye features para GAMM:
       - DIA_COSECHA      -> spline (bs, df=5)
       - ANIO_FRAC        -> spline (bs, df=4) [continuo: year + doy/365.25]
       - KG_HA            -> spline (bs, df=4)
       - FUNDO            -> random intercept
4. Cross-valida el corrector con StratifiedKFold(FUNDO_FORMATO, 5 folds),
   mismo esquema que el nested CV del training.
5. Para cada fold: ajusta MixedLM en train, predice correccion en test,
   compara MAE(y, base + correccion) vs MAE(y, base).
6. Decide gate:
       - Δ MAE ≤ -1.0%   ->  VERDE (proceder a Phase 1)
       - Δ MAE entre [-1%, +0.5%] -> GRIS (marginal, abortar)
       - Δ MAE > +0.5%   ->  ROJO (corrector empeora, abortar)

Uso
---
    # default: usa el champion mas reciente del run actual
    python -m scripts.validate_gamm_residuals --variety POP

    # version especifica
    python -m scripts.validate_gamm_residuals --variety POP --model lgb --version 2

    # con plot de splines aprendidos
    python -m scripts.validate_gamm_residuals --variety POP --plot

Output
------
    Console: tabla por fold + decision final
    artifacts/gamm_phase0_report_<variety>_<model>_v<n>.json
    [opcional --plot] artifacts/gamm_phase0_splines_<variety>_<model>_v<n>.png
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import StratifiedKFold

# Suprimimos warnings de convergencia de statsmodels para no llenar el log;
# el script los reporta como "convergence failure" en la tabla por fold.
warnings.filterwarnings("ignore", category=UserWarning, module="statsmodels")
warnings.filterwarnings("ignore", category=RuntimeWarning, module="statsmodels")


# Importes locales del proyecto. La validacion de feature_engineering esta
# definida en config; aqui solo necesitamos cargar (X, y) raw + el champion.
from src.config import ARTIFACTS_DIR, RANDOM_STATE, TARGET
from src.step_01_load.data_loader import load_data


# Hiperparametros del corrector GAMM. Conservadores para Phase 0:
#   - Solo DIA_COSECHA y ANIO_FRAC: features TEMPORALES suaves donde el arbol
#     hace escalones. KG/HA NO se incluye porque es feature core del champion
#     -- el corrector seria redundante (el arbol ya captura su efecto).
#   - n_knots=3, degree=2: el minimo necesario para suavidad sin multicolinealidad.
#     SplineTransformer(n_knots=3, degree=2, include_bias=False) -> 4 features
#     por columna = 8 features + 1 intercepto = 9 features totales. Para grupos
#     FUNDO de 4-16 niveles, esto evita Hessian no-PSD.
SPLINE_N_KNOTS: int = 3
SPLINE_DEGREE: int = 2
N_OUTER_FOLDS: int = 5

# Decision gates (porcentaje sobre MAE base). Coherente con el plan del
# proyecto archivado en project_gamm_plan.md (Fase 0 spec).
GATE_GREEN_PCT: float = -1.0   # Δ ≤ -1% -> VERDE
GATE_RED_PCT: float = +0.5     # Δ > +0.5% -> ROJO (en medio: GRIS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_artifact_paths(
    variety: str,
    model: str,
    version: Optional[int],
) -> Tuple[Path, Path]:
    """Devuelve (pipeline_path, oof_path). Si version=None busca la maxima."""
    pattern_pipeline = f"final_pipeline_{variety}_{model}_v*.joblib"
    pattern_oof = f"oof_{variety}_{model}_v*.npz"

    if version is not None:
        p = ARTIFACTS_DIR / f"final_pipeline_{variety}_{model}_v{version}.joblib"
        o = ARTIFACTS_DIR / f"oof_{variety}_{model}_v{version}.npz"
        if not p.exists():
            raise FileNotFoundError(
                f"No existe {p.name}. Disponibles: "
                f"{sorted(ARTIFACTS_DIR.glob(pattern_pipeline))}"
            )
        if not o.exists():
            raise FileNotFoundError(
                f"No existe {o.name}. Re-entrenar con la version actual de "
                f"single_run.py (que persiste OOF a .npz)."
            )
        return p, o

    # Buscar la version maxima DONDE AMBOS (pipeline + OOF) existan. Versiones
    # antiguas pueden no tener OOF persistido (single_run.py empezo a
    # persistirlos en una version posterior). Filtrar antes de tomar el max.
    candidates = sorted(ARTIFACTS_DIR.glob(pattern_pipeline))
    if not candidates:
        raise FileNotFoundError(
            f"No se encontro ningun pipeline para {variety}/{model} en {ARTIFACTS_DIR}"
        )
    # Ordenar por version numerica (no alfabetica: v10 > v9)
    def _ver(p: Path) -> int:
        try:
            return int(p.stem.rsplit("_v", 1)[-1])
        except ValueError:
            return 0
    candidates_with_oof = [
        p for p in candidates
        if (ARTIFACTS_DIR / f"oof_{variety}_{model}_v{_ver(p)}.npz").exists()
    ]
    if not candidates_with_oof:
        raise FileNotFoundError(
            f"Hay {len(candidates)} pipeline(s) para {variety}/{model} pero "
            f"ninguno tiene OOF persistido. Re-entrenar con la version actual "
            f"de single_run.py (que persiste OOF a artifacts/oof_*.npz)."
        )
    pipeline_path = max(candidates_with_oof, key=_ver)
    version_int = _ver(pipeline_path)
    oof_path = ARTIFACTS_DIR / f"oof_{variety}_{model}_v{version_int}.npz"
    return pipeline_path, oof_path


def _build_gamm_features(X_raw: pd.DataFrame) -> pd.DataFrame:
    """Construye features para el corrector. NO modifica X_raw; devuelve df nuevo.

    Renombra columnas con caracteres especiales (KG/HA, %INDUS) para que
    patsy las acepte en formulas (no permite '/', '%', etc).
    """
    df = pd.DataFrame(index=X_raw.index)
    df["DIA_COSECHA"] = pd.to_numeric(X_raw["DIA_COSECHA"], errors="coerce")
    df["KG_HA"] = pd.to_numeric(X_raw["KG/HA"], errors="coerce")
    df["FUNDO"] = X_raw["FUNDO"].astype(str)
    df["FORMATO"] = X_raw["FORMATO"].astype(str)

    # ANIO_FRAC: continuo, captura drift temporal sin discretizar.
    fechas = pd.to_datetime(X_raw["FECHA"], errors="coerce")
    df["ANIO_FRAC"] = fechas.dt.year + fechas.dt.dayofyear / 365.25

    # Imputar NaN con mediana del fold de train (proxy: mediana global aqui;
    # MixedLM no acepta NaN). Para Phase 0 la aproximacion es razonable.
    for col in ["DIA_COSECHA", "KG_HA", "ANIO_FRAC"]:
        df[col] = df[col].fillna(df[col].median())
    return df


def _fit_gamm_one_fold(
    df_train: pd.DataFrame,
    residuos_train: np.ndarray,
) -> Tuple[Optional[object], Optional[str]]:
    """Ajusta MixedLM con splines via SplineTransformer (sklearn).

    Implementacion:
        - SplineTransformer fittea knots en train (estables, reusables en test).
        - MixedLM ajusta sobre features [intercepto + spline_dia + spline_anio +
          spline_kg_ha], con random intercept por FUNDO.
        - Guardamos los SplineTransformers en `result._gamm_splines` para
          predict.

    Decision de no usar patsy.bs: stateful transforms de patsy se rompen al
    re-evaluar en test data (NotImplementedError en bs.eval cuando design_info
    se reutiliza). SplineTransformer es la alternativa sklearn-nativa: fit
    en train, transform en test, knots consistentes garantizados.
    """
    from sklearn.preprocessing import SplineTransformer
    from statsmodels.regression.mixed_linear_model import MixedLM

    # Solo features TEMPORALES suaves. KG_HA omitido a proposito: ya es feature
    # core del champion, agregar su spline aqui daria multicolinealidad sin
    # senal incremental.
    spline_cols = ["DIA_COSECHA", "ANIO_FRAC"]
    splines: Dict[str, object] = {}
    X_parts: List[np.ndarray] = []

    try:
        for col in spline_cols:
            st = SplineTransformer(
                n_knots=SPLINE_N_KNOTS,
                degree=SPLINE_DEGREE,
                include_bias=False,
            ).fit(df_train[[col]])
            splines[col] = st
            X_parts.append(st.transform(df_train[[col]]))
    except Exception as exc:
        return None, f"splines_fit: {exc}"

    # Intercepto manual (MixedLM no lo agrega por default si exog ya tiene
    # columnas; lo incluimos explicito para evitar sorpresas).
    X_design = np.column_stack([np.ones(len(df_train))] + X_parts)

    try:
        # method='lbfgs' mas robusto que default 'newton' para grupos chicos.
        model = MixedLM(
            endog=residuos_train,
            exog=X_design,
            groups=df_train["FUNDO"].values,
        )
        result = model.fit(method="lbfgs", maxiter=200, disp=False)
    except Exception as exc:
        return None, f"fit: {exc}"

    result._gamm_splines = splines
    return result, None


def _predict_correction(
    fitted: object,
    df_test: pd.DataFrame,
) -> np.ndarray:
    """Calcula correccion (fixed effects only) usando los SplineTransformers
    fitados en train. Random intercepts no se aplican porque puede haber
    grupos en test no vistos en train (fundo nuevo) y queremos predict robusto.
    """
    X_parts: List[np.ndarray] = []
    for col, st in fitted._gamm_splines.items():
        X_parts.append(st.transform(df_test[[col]]))
    X_design = np.column_stack([np.ones(len(df_test))] + X_parts)
    # fe_params puede ser ndarray (cuando exog fue numpy) o Series
    # (cuando exog fue DataFrame). np.asarray normaliza a ndarray.
    return X_design @ np.asarray(fitted.fe_params)


# ---------------------------------------------------------------------------
# Flujo principal
# ---------------------------------------------------------------------------


def run_phase0(
    variety: str,
    model: str,
    version: Optional[int],
    plot: bool,
) -> Dict[str, object]:
    """Ejecuta Phase 0 y devuelve dict serializable con el reporte."""
    # 1. Resolver paths del champion + OOF
    pipeline_path, oof_path = _resolve_artifact_paths(variety, model, version)
    print(f"Champion:  {pipeline_path.name}")
    print(f"OOF:       {oof_path.name}")
    print()

    # 2. Cargar OOF + raw data (necesario para FUNDO + features splines)
    oof = np.load(oof_path)
    y_true = oof["y_true"]
    y_pred = oof["y_pred"]

    X_raw, _ = load_data(sheet=variety)
    if len(X_raw) != len(y_true):
        raise RuntimeError(
            f"Desalineacion OOF vs X_raw: {len(y_true)} vs {len(X_raw)}. "
            f"El OOF se persistio con un dataset distinto al actual."
        )

    # Filtrar NaN en OOF (cold-start del primer fold del nested CV: algunas
    # filas pueden no tener pred OOF si quedaron fuera de todos los folds).
    valid = ~np.isnan(y_true) & ~np.isnan(y_pred)
    n_drop = int((~valid).sum())
    if n_drop:
        print(f"[warning] {n_drop} filas con OOF NaN (excluidas)")

    df_features = _build_gamm_features(X_raw)
    df_features = df_features.loc[valid].reset_index(drop=True)
    y_true = y_true[valid]
    y_pred = y_pred[valid]
    n = len(df_features)

    # 3. Residuos en log-space (consistente con TransformedTargetRegressor)
    residuos_log = np.log1p(y_true) - np.log1p(y_pred)
    print(f"Residuos log-space: n={n} | mean={residuos_log.mean():+.4f} | "
          f"std={residuos_log.std():.4f} | "
          f"max_abs={np.abs(residuos_log).max():.4f}")
    print()

    # 4. Cross-validation con StratifiedKFold(FUNDO_FORMATO)
    strat_label = df_features["FUNDO"] + "_" + df_features["FORMATO"]
    cv = StratifiedKFold(
        n_splits=N_OUTER_FOLDS, shuffle=True, random_state=RANDOM_STATE,
    )

    fold_results: List[dict] = []
    print(f"{'Fold':>4} | {'MAE_base':>9} | {'MAE_corr':>9} | "
          f"{'Δ_abs':>9} | {'Δ_pct':>7} | status")
    print("-" * 70)

    for fold_idx, (tr_i, te_i) in enumerate(cv.split(df_features, strat_label), start=1):
        df_tr = df_features.iloc[tr_i]
        df_te = df_features.iloc[te_i]
        residuos_tr = residuos_log[tr_i]
        y_true_te = y_true[te_i]
        y_pred_te = y_pred[te_i]

        fitted, err = _fit_gamm_one_fold(df_tr, residuos_tr)
        mae_base = float(mean_absolute_error(y_true_te, y_pred_te))

        if fitted is None:
            print(f"{fold_idx:>4} | {mae_base:9.4f} | {'-':>9} | {'-':>9} | "
                  f"{'-':>7} | FAIL ({err[:40]})")
            fold_results.append({
                "fold": fold_idx,
                "converged": False,
                "error": err,
                "mae_base": mae_base,
            })
            continue

        # Predecir correccion + aplicar
        try:
            correction_log = _predict_correction(fitted, df_te)
        except Exception as exc:
            print(f"{fold_idx:>4} | {mae_base:9.4f} | {'-':>9} | {'-':>9} | "
                  f"{'-':>7} | PREDICT_FAIL ({str(exc)[:40]})")
            fold_results.append({
                "fold": fold_idx,
                "converged": True,
                "predict_error": str(exc),
                "mae_base": mae_base,
            })
            continue

        y_pred_corrected_log = np.log1p(y_pred_te) + correction_log
        y_pred_corrected = np.expm1(y_pred_corrected_log)
        mae_corr = float(mean_absolute_error(y_true_te, y_pred_corrected))
        delta_abs = mae_corr - mae_base
        delta_pct = (delta_abs / mae_base) * 100.0

        status = "OK" if delta_abs < 0 else ("FLAT" if abs(delta_pct) < 0.5 else "WORSE")
        print(f"{fold_idx:>4} | {mae_base:9.4f} | {mae_corr:9.4f} | "
              f"{delta_abs:+9.4f} | {delta_pct:+6.2f}% | {status}")

        fold_results.append({
            "fold": fold_idx,
            "converged": True,
            "mae_base": mae_base,
            "mae_corrected": mae_corr,
            "delta_abs": delta_abs,
            "delta_pct": delta_pct,
            "n_test": int(len(te_i)),
        })

    # 5. Agregacion + decision
    converged_folds = [r for r in fold_results if r.get("converged") and "delta_pct" in r]
    print("-" * 70)
    if not converged_folds:
        decision = "ROJO"
        decision_reason = "Ningun fold convergio: GAMM no aplica para estos datos"
        delta_pct_mean = None
    else:
        delta_pct_mean = float(np.mean([r["delta_pct"] for r in converged_folds]))
        if delta_pct_mean <= GATE_GREEN_PCT:
            decision = "VERDE"
            decision_reason = (
                f"Δ MAE = {delta_pct_mean:+.2f}% mejor que threshold "
                f"{GATE_GREEN_PCT}% -> proceder a Phase 1 (implementar corrector)"
            )
        elif delta_pct_mean >= GATE_RED_PCT:
            decision = "ROJO"
            decision_reason = (
                f"Δ MAE = {delta_pct_mean:+.2f}% peor que threshold "
                f"{GATE_RED_PCT}% -> abortar GAMM, residuos no aprovechables"
            )
        else:
            decision = "GRIS"
            decision_reason = (
                f"Δ MAE = {delta_pct_mean:+.2f}% en zona marginal "
                f"[{GATE_GREEN_PCT}%, {GATE_RED_PCT}%] -> abortar (no justifica complejidad)"
            )

    print(f"\nFolds convergidos: {len(converged_folds)}/{N_OUTER_FOLDS}")
    if delta_pct_mean is not None:
        print(f"Δ MAE promedio:   {delta_pct_mean:+.2f}%")
    print(f"\nDECISION: {decision}")
    print(f"  {decision_reason}")

    # 6. Reporte serializable
    report = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "variety": variety,
        "model": model,
        "champion_pipeline": pipeline_path.name,
        "oof_file": oof_path.name,
        "n_rows": int(n),
        "n_dropped_nan": n_drop,
        "residuos_log_stats": {
            "mean": float(residuos_log.mean()),
            "std": float(residuos_log.std()),
            "max_abs": float(np.abs(residuos_log).max()),
        },
        "config": {
            "spline_n_knots": SPLINE_N_KNOTS,
            "spline_degree": SPLINE_DEGREE,
            "spline_cols": ["DIA_COSECHA", "ANIO_FRAC"],
            "n_outer_folds": N_OUTER_FOLDS,
            "gate_green_pct": GATE_GREEN_PCT,
            "gate_red_pct": GATE_RED_PCT,
        },
        "fold_results": fold_results,
        "delta_pct_mean": delta_pct_mean,
        "decision": decision,
        "decision_reason": decision_reason,
    }

    # 7. Persistir reporte
    version_int = int(pipeline_path.stem.rsplit("_v", 1)[-1])
    out_path = ARTIFACTS_DIR / f"gamm_phase0_report_{variety}_{model}_v{version_int}.json"
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"\nReporte guardado: {out_path.name}")

    if plot and decision == "VERDE":
        # Plot de splines aprendidos solo si VERDE (sino no vale la pena)
        try:
            _plot_splines(df_features, residuos_log, variety, model, version_int)
        except Exception as exc:
            print(f"[warning] no se pudo generar plot: {exc}")

    return report


def _plot_splines(
    df_features: pd.DataFrame,
    residuos_log: np.ndarray,
    variety: str,
    model: str,
    version: int,
) -> None:
    """Visualiza la forma aprendida de los splines (solo si Phase 0 = VERDE)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fitted, _ = _fit_gamm_one_fold(df_features, residuos_log)
    if fitted is None:
        return

    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    feature_specs = [
        ("DIA_COSECHA", "Día de cosecha"),
        ("ANIO_FRAC", "Año (continuo)"),
    ]
    for ax, (col, label) in zip(axes, feature_specs):
        x_grid = np.linspace(df_features[col].min(), df_features[col].max(), 100)
        df_grid = df_features.iloc[[0]].copy()
        df_grid = pd.concat([df_grid] * 100).reset_index(drop=True)
        df_grid[col] = x_grid
        # Holdear las otras dos features en su mediana para visualizar el spline 1D
        for other_col, _ in feature_specs:
            if other_col != col:
                df_grid[other_col] = df_features[other_col].median()
        try:
            y_grid = _predict_correction(fitted, df_grid)
            ax.plot(x_grid, y_grid, linewidth=2)
            ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
            ax.set_xlabel(label)
            ax.set_ylabel("Corrección (log-space)")
            ax.set_title(f"Spline aprendido: {col}")
            ax.grid(alpha=0.3)
        except Exception:
            ax.text(0.5, 0.5, "no disponible", ha="center", va="center",
                    transform=ax.transAxes)

    fig.suptitle(f"GAMM Phase 0 — {variety}/{model}_v{version}")
    fig.tight_layout()
    out = ARTIFACTS_DIR / f"gamm_phase0_splines_{variety}_{model}_v{version}.png"
    fig.savefig(out, dpi=100, bbox_inches="tight")
    plt.close(fig)
    print(f"Plot guardado:    {out.name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 0 GAMM: validacion standalone del corrector de residuos.",
    )
    parser.add_argument("--variety", default="POP",
                        help="Variedad a validar (default: POP)")
    parser.add_argument("--model", default="lgb",
                        help="Modelo champion (default: lgb)")
    parser.add_argument("--version", type=int, default=None,
                        help="Version del champion (default: la ultima)")
    parser.add_argument("--plot", action="store_true",
                        help="Generar PNG con splines aprendidos (solo si VERDE)")
    args = parser.parse_args()

    try:
        report = run_phase0(args.variety, args.model, args.version, args.plot)
    except FileNotFoundError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception:
        import traceback
        print("\nERROR inesperado:", file=sys.stderr)
        traceback.print_exc()
        sys.exit(3)

    sys.exit(0 if report["decision"] == "VERDE" else 1)


if __name__ == "__main__":
    main()

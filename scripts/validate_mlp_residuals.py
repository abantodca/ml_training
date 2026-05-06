"""Phase 0 MLP: validacion standalone del corrector de residuos via red neuronal.

Misma logica que `validate_gamm_residuals.py` pero usando un MLP (red neuronal
chica) como corrector. Razon: GAMM (aditivo) dio ROJO; MLP puede capturar
INTERACCIONES no-lineales multi-feature que GAMM por construccion no modela.

Diseno del MLP corrector
------------------------
- Arquitectura: 1-2 capas ocultas pequenas (16-32 unidades). Red chica
  intencionalmente para limitar overfit en residuos.
- Regularizacion: alpha alto (L2) + early_stopping con validation_fraction.
- Features: subset de las raw + ANIO_FRAC. NO usamos las 63 features del
  champion porque el corrector debe APRENDER LO QUE EL ARBOL NO CAPTURO,
  no replicarlo. Usar features estructurales chicas:
      DIA_COSECHA, ANIO_FRAC, KG_HA, %INDUS, DPC, FUNDO_idx, FORMATO_idx
- Standarizacion: StandardScaler antes del MLP (NN necesita features escaladas).

Decision gate (igual que GAMM)
------------------------------
    - Δ MAE ≤ -1.0%   ->  VERDE (proceder a Phase 1)
    - Δ MAE en (-1%, +0.5%)  ->  GRIS (marginal, abortar)
    - Δ MAE > +0.5%   ->  ROJO (corrector empeora, abortar)

Uso
---
    python -m scripts.validate_mlp_residuals --variety POP
    python -m scripts.validate_mlp_residuals --variety POP --hidden 32 16
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
from sklearn.neural_network import MLPRegressor
from sklearn.preprocessing import StandardScaler

# Suprimir ConvergenceWarning de MLPRegressor cuando max_iter alcanzado
warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")
warnings.filterwarnings("ignore", category=Warning, module="sklearn.neural_network")

from src.config import ARTIFACTS_DIR, RANDOM_STATE
from src.step_01_load.data_loader import load_data


# Hiperparametros conservadores. Si Phase 0 da verde, en Phase 1 podriamos
# tunear con Optuna; aqui un default sano para decidir go/no-go.
DEFAULT_HIDDEN: Tuple[int, ...] = (32, 16)
MAX_ITER: int = 500
ALPHA: float = 0.01            # L2 penalty (alta para regularizar)
LEARNING_RATE_INIT: float = 0.001
EARLY_STOPPING: bool = True
VALIDATION_FRACTION: float = 0.15
N_OUTER_FOLDS: int = 5

GATE_GREEN_PCT: float = -1.0
GATE_RED_PCT: float = +0.5


def _resolve_artifact_paths(
    variety: str,
    model: str,
    version: Optional[int],
) -> Tuple[Path, Path]:
    pattern_pipeline = f"final_pipeline_{variety}_{model}_v*.joblib"

    if version is not None:
        p = ARTIFACTS_DIR / f"final_pipeline_{variety}_{model}_v{version}.joblib"
        o = ARTIFACTS_DIR / f"oof_{variety}_{model}_v{version}.npz"
        if not p.exists() or not o.exists():
            raise FileNotFoundError(f"No existen pipeline u OOF para v{version}")
        return p, o

    candidates = sorted(ARTIFACTS_DIR.glob(pattern_pipeline))
    if not candidates:
        raise FileNotFoundError(f"No hay pipelines para {variety}/{model}")

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
            f"Hay {len(candidates)} pipeline(s) pero ninguno tiene OOF persistido."
        )
    pipeline_path = max(candidates_with_oof, key=_ver)
    version_int = _ver(pipeline_path)
    oof_path = ARTIFACTS_DIR / f"oof_{variety}_{model}_v{version_int}.npz"
    return pipeline_path, oof_path


def _build_mlp_features(X_raw: pd.DataFrame) -> pd.DataFrame:
    """Subset de features + encoding ordinal para categoricas.

    NO usa las 63 features del champion: queremos que el MLP aprenda lo que
    el arbol omitio, no replicarlo. Stick con un set chico relevante.
    """
    df = pd.DataFrame(index=X_raw.index)
    df["KG_HA"] = pd.to_numeric(X_raw["KG/HA"], errors="coerce")
    df["PCT_INDUS"] = pd.to_numeric(X_raw["%INDUS"], errors="coerce")
    df["DPC"] = pd.to_numeric(X_raw["DPC"], errors="coerce")
    df["P_BAYA"] = pd.to_numeric(X_raw["P/BAYA"], errors="coerce")
    df["HA"] = pd.to_numeric(X_raw["HA"], errors="coerce")
    df["DIA_COSECHA"] = pd.to_numeric(X_raw["DIA_COSECHA"], errors="coerce")

    fechas = pd.to_datetime(X_raw["FECHA"], errors="coerce")
    df["ANIO_FRAC"] = fechas.dt.year + fechas.dt.dayofyear / 365.25
    # Cyclic encoding para mes (NN lo aprende mejor con sin/cos que con int)
    month = fechas.dt.month.astype(float)
    df["MES_SIN"] = np.sin(2 * np.pi * month / 12.0)
    df["MES_COS"] = np.cos(2 * np.pi * month / 12.0)

    # Categoricas como ordinales (MLP las aprende como continuas; one-hot
    # explotaria el numero de features sin aporte para 4-5 niveles)
    fundo_codes = pd.Categorical(X_raw["FUNDO"].astype(str)).codes
    formato_codes = pd.Categorical(X_raw["FORMATO"].astype(str)).codes
    df["FUNDO_idx"] = fundo_codes.astype(float)
    df["FORMATO_idx"] = formato_codes.astype(float)

    # Imputar NaN con mediana (NN no soporta NaN)
    for col in df.columns:
        df[col] = df[col].fillna(df[col].median())
    return df


def run_phase0_mlp(
    variety: str,
    model: str,
    version: Optional[int],
    hidden_layers: Tuple[int, ...],
) -> Dict[str, object]:
    pipeline_path, oof_path = _resolve_artifact_paths(variety, model, version)
    print(f"Champion:  {pipeline_path.name}")
    print(f"OOF:       {oof_path.name}")
    print(f"MLP arch:  hidden={hidden_layers} | alpha={ALPHA} | "
          f"early_stop={EARLY_STOPPING}")
    print()

    oof = np.load(oof_path)
    y_true = oof["y_true"]
    y_pred = oof["y_pred"]

    X_raw, _ = load_data(sheet=variety)
    if len(X_raw) != len(y_true):
        raise RuntimeError(f"Desalineacion OOF vs X_raw")

    valid = ~np.isnan(y_true) & ~np.isnan(y_pred)
    n_drop = int((~valid).sum())
    if n_drop:
        print(f"[warning] {n_drop} filas con OOF NaN (excluidas)")

    df_features = _build_mlp_features(X_raw)
    df_features = df_features.loc[valid].reset_index(drop=True)
    y_true = y_true[valid]
    y_pred = y_pred[valid]
    n = len(df_features)

    # Residuos en log-space (consistente con TransformedTargetRegressor)
    residuos_log = np.log1p(y_true) - np.log1p(y_pred)
    print(f"Residuos log-space: n={n} | mean={residuos_log.mean():+.4f} | "
          f"std={residuos_log.std():.4f}")
    print()

    # CV con stratification por FUNDO_FORMATO (mismo que training)
    strat_label = (
        X_raw.loc[valid, "FUNDO"].astype(str).reset_index(drop=True)
        + "_" + X_raw.loc[valid, "FORMATO"].astype(str).reset_index(drop=True)
    )
    cv = StratifiedKFold(
        n_splits=N_OUTER_FOLDS, shuffle=True, random_state=RANDOM_STATE,
    )

    fold_results: List[dict] = []
    print(f"{'Fold':>4} | {'MAE_base':>9} | {'MAE_corr':>9} | "
          f"{'Δ_abs':>9} | {'Δ_pct':>7} | status")
    print("-" * 70)

    for fold_idx, (tr_i, te_i) in enumerate(cv.split(df_features, strat_label), start=1):
        X_tr = df_features.iloc[tr_i].values
        X_te = df_features.iloc[te_i].values
        residuos_tr = residuos_log[tr_i]
        y_true_te = y_true[te_i]
        y_pred_te = y_pred[te_i]

        # StandardScaler -> MLP (en pipeline implicito)
        scaler = StandardScaler().fit(X_tr)
        X_tr_s = scaler.transform(X_tr)
        X_te_s = scaler.transform(X_te)

        try:
            mlp = MLPRegressor(
                hidden_layer_sizes=hidden_layers,
                max_iter=MAX_ITER,
                alpha=ALPHA,
                learning_rate_init=LEARNING_RATE_INIT,
                early_stopping=EARLY_STOPPING,
                validation_fraction=VALIDATION_FRACTION,
                random_state=RANDOM_STATE + fold_idx,
                solver="adam",
            )
            mlp.fit(X_tr_s, residuos_tr)
        except Exception as exc:
            mae_base = float(mean_absolute_error(y_true_te, y_pred_te))
            print(f"{fold_idx:>4} | {mae_base:9.4f} | {'-':>9} | {'-':>9} | "
                  f"{'-':>7} | FAIL ({str(exc)[:30]})")
            fold_results.append({
                "fold": fold_idx,
                "converged": False,
                "error": str(exc),
                "mae_base": mae_base,
            })
            continue

        # Predecir correccion + aplicar
        correction_log = mlp.predict(X_te_s)
        y_pred_corrected_log = np.log1p(y_pred_te) + correction_log
        y_pred_corrected = np.expm1(y_pred_corrected_log)

        mae_base = float(mean_absolute_error(y_true_te, y_pred_te))
        mae_corr = float(mean_absolute_error(y_true_te, y_pred_corrected))
        delta_abs = mae_corr - mae_base
        delta_pct = (delta_abs / mae_base) * 100.0

        status = "OK" if delta_abs < 0 else ("FLAT" if abs(delta_pct) < 0.5 else "WORSE")
        print(f"{fold_idx:>4} | {mae_base:9.4f} | {mae_corr:9.4f} | "
              f"{delta_abs:+9.4f} | {delta_pct:+6.2f}% | {status} | "
              f"iters={mlp.n_iter_}")

        fold_results.append({
            "fold": fold_idx,
            "converged": True,
            "mae_base": mae_base,
            "mae_corrected": mae_corr,
            "delta_abs": delta_abs,
            "delta_pct": delta_pct,
            "n_iter": int(mlp.n_iter_),
            "n_test": int(len(te_i)),
        })

    # Decision gate
    converged_folds = [r for r in fold_results if r.get("converged") and "delta_pct" in r]
    print("-" * 70)
    if not converged_folds:
        decision = "ROJO"
        decision_reason = "Ningun fold convergio"
        delta_pct_mean = None
    else:
        delta_pct_mean = float(np.mean([r["delta_pct"] for r in converged_folds]))
        if delta_pct_mean <= GATE_GREEN_PCT:
            decision = "VERDE"
            decision_reason = f"Δ MAE = {delta_pct_mean:+.2f}% mejor que {GATE_GREEN_PCT}% -> proceder"
        elif delta_pct_mean >= GATE_RED_PCT:
            decision = "ROJO"
            decision_reason = f"Δ MAE = {delta_pct_mean:+.2f}% peor que {GATE_RED_PCT}% -> abortar"
        else:
            decision = "GRIS"
            decision_reason = f"Δ MAE = {delta_pct_mean:+.2f}% marginal -> abortar"

    print(f"\nFolds convergidos: {len(converged_folds)}/{N_OUTER_FOLDS}")
    if delta_pct_mean is not None:
        print(f"Δ MAE promedio:   {delta_pct_mean:+.2f}%")
    print(f"\nDECISION: {decision}")
    print(f"  {decision_reason}")

    report = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "corrector_type": "MLP",
        "variety": variety,
        "model": model,
        "champion_pipeline": pipeline_path.name,
        "oof_file": oof_path.name,
        "n_rows": int(n),
        "n_dropped_nan": n_drop,
        "config": {
            "hidden_layers": list(hidden_layers),
            "max_iter": MAX_ITER,
            "alpha": ALPHA,
            "learning_rate_init": LEARNING_RATE_INIT,
            "early_stopping": EARLY_STOPPING,
            "validation_fraction": VALIDATION_FRACTION,
            "n_outer_folds": N_OUTER_FOLDS,
        },
        "fold_results": fold_results,
        "delta_pct_mean": delta_pct_mean,
        "decision": decision,
        "decision_reason": decision_reason,
    }

    version_int = int(pipeline_path.stem.rsplit("_v", 1)[-1])
    out_path = ARTIFACTS_DIR / f"mlp_phase0_report_{variety}_{model}_v{version_int}.json"
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"\nReporte guardado: {out_path.name}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 0 MLP: validacion standalone del corrector via red neuronal.",
    )
    parser.add_argument("--variety", default="POP")
    parser.add_argument("--model", default="lgb")
    parser.add_argument("--version", type=int, default=None)
    parser.add_argument(
        "--hidden", nargs="+", type=int, default=list(DEFAULT_HIDDEN),
        help="Tamanos de capas ocultas (e.g. --hidden 32 16). Default: 32 16",
    )
    args = parser.parse_args()

    try:
        report = run_phase0_mlp(
            args.variety, args.model, args.version, tuple(args.hidden),
        )
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

"""SHAP-based feature importance del campeon + clasificacion.

Modulo central reusable desde el training pipeline (`variety_runner.py`)
y desde el script post-hoc (`scripts/analyze_features.py`). Computa
SHAP values via `TreeExplainer` sobre los K modelos del OOF ensemble,
agrega por feature RAW (no por feature interna post-pipeline), y
clasifica cada feature en uno de cuatro buckets accionables:

    core | util | podable | ruido

Por que SHAP TreeExplainer y no permutation_importance:
  - Aprovecha la estructura de los arboles -> calculo exacto y rapido
    (~30s-2min vs 3-8min de permutation).
  - Da DIRECCION (positivo/negativo) ademas de magnitud, asi sabemos
    si una feature empuja la prediccion arriba o abajo en promedio.
  - Da contribuciones POR FILA (no solo global), lo que habilita
    beeswarm/dependence plots para diagnostico fino.

Decisiones de diseno:
  1. Estructura del champion saved:
        StackedRegressor (si stacking on)
          .base_pipeline_ -> OOFEnsembleRegressor
            .models_ -> [Pipeline(preprocessor + xgb_or_lgb)] * K
        OOFEnsembleRegressor (si stacking off)  <-- caso futuro default
          .models_ -> idem
        Pipeline (legacy K=1)
     Navegamos hasta extraer las K (preprocessor, regressor) tuples.

  2. SHAP values por raw feature: las features INTERNAS (post-pipeline)
     incluyen MES_SIN/COS, FORMATO_X, FUNDO_Y, <col>__ISNAN. Para que la
     decision sea sobre features que el cliente ENVIA (las 40 raw),
     sumamos los SHAP de cada grupo de derivadas hacia su raw fuente.

  3. Promedio sobre los K modelos: predict() promedia, asi que SHAP
     promediado da la contribucion correcta para la prediccion final.

  4. Para acelerar en datasets grandes, soportamos sub-muestreo via
     `n_sample`. SHAP es global -> 2000 filas alcanzan para una buena
     estimacion sin esperar el 10x del dataset completo.
"""
from __future__ import annotations

import io
import logging
import re
from base64 import b64encode
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# Umbrales de clasificacion como SHARE del importance total acumulado.
# Esta normalizacion es invariante a la escala del target: funciona igual
# si SHAP esta en espacio log (TransformedTargetRegressor) o en espacio
# original. La idea: "esta feature aporta X% del total de explicacion".
SHARE_CORE: float = 0.05      # >= 5% del importance total = nucleo
SHARE_UTIL: float = 0.01      # >= 1% del importance total = util
SHARE_PRUNABLE: float = 0.001  # >= 0.1% = podable
# < 0.1% -> ruido (esencialmente cero contribucion)

STATUS_CORE: str = "core"          # share >= 5% -> sostiene el modelo
STATUS_UTIL: str = "util"          # 1% <= share < 5% -> aporta, no critica
STATUS_PRUNABLE: str = "podable"   # 0.1% <= share < 1% -> el modelo casi no la usa
STATUS_NOISE: str = "ruido"        # share < 0.1% -> contribucion despreciable


# Patrones de mapeo internal -> raw para el preprocessor de este proyecto.
_FECHA_DERIVED_PREFIXES: Tuple[str, ...] = (
    "MES_SIN", "MES_COS",     # incluye MES_SIN2, MES_SIN3, MES_COS2, MES_COS3
    "SEMANA_SIN", "SEMANA_COS",
    "DIA_SEM_SIN", "DIA_SEM_COS",
    "TEMPORADA_ALTA", "TEMPORADA_BAJA",
)
_FECHA_DERIVED_EXACT: Tuple[str, ...] = ("ANIO",)


def _classify(share: float) -> str:
    """Clasifica una feature por su SHARE del importance total acumulado.

    `share` es importance_mean / sum(importance_mean), en [0, 1].
    Invariante a la escala absoluta del SHAP (que cambia con
    TransformedTargetRegressor / log-target).
    """
    if share >= SHARE_CORE:
        return STATUS_CORE
    if share >= SHARE_UTIL:
        return STATUS_UTIL
    if share >= SHARE_PRUNABLE:
        return STATUS_PRUNABLE
    return STATUS_NOISE


@dataclass(frozen=True)
class FeatureImportanceResult:
    """Resultado completo de SHAP feature importance.

    df             : DataFrame ordenado por importance_mean DESC. Columnas:
                     rank, feature, importance_mean, importance_std,
                     direction_mean, status.
    n_samples      : numero de filas usadas para computar SHAP.
    n_models       : K modelos del OOF ensemble (SHAP promediado).
    method         : 'shap_tree' (TreeExplainer).
    mae_base       : MAE del modelo en el dataset evaluado, antes del SHAP.
    beeswarm_b64   : PNG de matplotlib codificado base64 (sin prefijo data:),
                     None si la generacion fallo.
    """

    df: pd.DataFrame
    n_samples: int
    n_models: int
    method: str
    mae_base: float
    beeswarm_b64: Optional[str] = None
    # SHAP values agregados por raw feature, dimensiones (n_samples, n_raw).
    # Lo guardamos para que el caller pueda hacer beeswarm/dependence si quiere
    # sin re-computar. None cuando viene del modo retro retorno mas chico.
    shap_by_raw: Optional[np.ndarray] = field(default=None, repr=False)
    raw_features: Optional[List[str]] = field(default=None, repr=False)

    @property
    def core_features(self) -> list[str]:
        return self.df.loc[self.df["status"] == STATUS_CORE, "feature"].tolist()

    @property
    def prunable_features(self) -> list[str]:
        return self.df.loc[self.df["status"] == STATUS_PRUNABLE, "feature"].tolist()

    @property
    def noise_features(self) -> list[str]:
        return self.df.loc[self.df["status"] == STATUS_NOISE, "feature"].tolist()

    def to_dict_summary(self) -> dict:
        """Resumen plano para inyectar como tags MLflow / KPIs ejecutivos."""
        n_total = len(self.df)
        return {
            "fi_method": self.method,
            "fi_n_features": n_total,
            "fi_n_samples": self.n_samples,
            "fi_n_models": self.n_models,
            "fi_n_core": int((self.df["status"] == STATUS_CORE).sum()),
            "fi_n_util": int((self.df["status"] == STATUS_UTIL).sum()),
            "fi_n_prunable": int((self.df["status"] == STATUS_PRUNABLE).sum()),
            "fi_n_noise": int((self.df["status"] == STATUS_NOISE).sum()),
            "fi_top1_feature": self.df.iloc[0]["feature"] if n_total else "",
            "fi_top1_importance": float(self.df.iloc[0]["importance_mean"]) if n_total else 0.0,
            "fi_mae_base": self.mae_base,
        }


# ---------------------------------------------------------------------------
# Extraccion del pipeline
# ---------------------------------------------------------------------------


def _unwrap_to_base(top_model):
    """Quita la capa de StackedRegressor si la hay, devuelve el base."""
    if hasattr(top_model, "base_pipeline_"):
        return top_model.base_pipeline_
    return top_model


def _unwrap_target_transformer(regressor):
    """Si el regressor es un TransformedTargetRegressor, devuelve el inner tree.

    TransformedTargetRegressor aplica log1p al target en fit y exp en predict.
    SHAP TreeExplainer NO soporta este wrapper; necesita el arbol crudo. La
    consecuencia es que los SHAP values quedan en ESPACIO LOG (contribucion a
    log(pred+1) en lugar de pred), pero el RANKING relativo y el SIGNO se
    preservan -- que es lo unico que la decision de poda usa.
    """
    if regressor is None:
        return None
    # sklearn.compose.TransformedTargetRegressor expone `.regressor_` tras fit.
    if hasattr(regressor, "regressor_") and not hasattr(regressor, "tree_"):
        # `tree_` es del DecisionTreeRegressor; el chequeo evita unwrap-ear de mas.
        cls_name = type(regressor).__name__
        if "TransformedTargetRegressor" in cls_name or "TargetTransform" in cls_name:
            return regressor.regressor_
    return regressor


def _extract_estimators(top_model) -> List[Tuple[Optional[object], object]]:
    """Devuelve [(preprocessor, tree_regressor), ...] del champion.

    Maneja: StackedRegressor -> OOFEnsembleRegressor -> K Pipelines ->
            TransformedTargetRegressor -> XGB/LGB tree.
    """
    base = _unwrap_to_base(top_model)

    # Caso A: OOFEnsembleRegressor con `models_` (K Pipelines).
    if hasattr(base, "models_") and isinstance(base.models_, (list, tuple)):
        out: List[Tuple[Optional[object], object]] = []
        for m in base.models_:
            if hasattr(m, "named_steps"):
                pp = m.named_steps.get("preprocessor")
                reg = m.named_steps.get("regressor")
                reg = _unwrap_target_transformer(reg)
                if reg is not None:
                    out.append((pp, reg))
            else:
                # Caso K=1 con base_pipeline = Pipeline: tomamos m directo.
                out.append((None, _unwrap_target_transformer(m)))
        if out:
            return out

    # Caso B: Pipeline directo con preprocessor + regressor.
    if hasattr(base, "named_steps"):
        pp = base.named_steps.get("preprocessor")
        reg = _unwrap_target_transformer(base.named_steps.get("regressor"))
        if reg is not None:
            return [(pp, reg)]

    # Caso C: regressor desnudo (poco probable).
    return [(None, _unwrap_target_transformer(base))]


def _internal_to_raw(internal_name: str, raw_columns: Sequence[str]) -> str:
    """Mapea un nombre de columna interna (post-pipeline) a su raw fuente.

    Reglas:
      1. Match exacto con una raw col -> esa raw.
      2. <raw>__ISNAN -> raw (flags de MissingFlagger).
      3. Una de las derivadas de FECHA -> 'FECHA'.
      4. Empieza con '<raw_categorica>_' (FORMATO_*, FUNDO_*) -> raw_categorica.
      5. Sino -> el propio internal_name (lag features y otros pasan tal cual).
    """
    if internal_name in raw_columns:
        return internal_name

    if internal_name.endswith("__ISNAN"):
        base = internal_name[: -len("__ISNAN")]
        if base in raw_columns:
            return base

    if internal_name in _FECHA_DERIVED_EXACT:
        return "FECHA" if "FECHA" in raw_columns else internal_name
    for prefix in _FECHA_DERIVED_PREFIXES:
        if internal_name == prefix or internal_name.startswith(prefix):
            # MES_SIN, MES_SIN2, MES_SIN3 todos -> FECHA
            return "FECHA" if "FECHA" in raw_columns else internal_name

    # Categoricas one-hot: raw_<valor>. Ordenamos por longitud descendente
    # para que 'FORMATO' venza a 'FORM' si alguna se llamara asi.
    for raw in sorted([c for c in raw_columns], key=len, reverse=True):
        if internal_name.startswith(f"{raw}_"):
            return raw

    return internal_name


def _build_internal_to_raw_map(
    internal_names: Iterable[str],
    raw_columns: Sequence[str],
) -> dict:
    return {name: _internal_to_raw(name, raw_columns) for name in internal_names}


# ---------------------------------------------------------------------------
# SHAP core
# ---------------------------------------------------------------------------


def _compute_shap_for_one(preprocessor, regressor, X) -> Tuple[np.ndarray, List[str]]:
    """Calcula SHAP values para UN tuple (preprocessor, regressor).

    Devuelve (shap_values, internal_feature_names).
    """
    import shap

    X_pp = preprocessor.transform(X) if preprocessor is not None else X
    if hasattr(X_pp, "columns"):
        internal_names = list(X_pp.columns)
        X_pp_arr = X_pp
    else:
        # Si la salida es ndarray, intentamos sacar nombres del preprocessor.
        try:
            internal_names = list(preprocessor.get_feature_names_out())
        except Exception:
            internal_names = [f"f{i}" for i in range(X_pp.shape[1])]
        X_pp_arr = pd.DataFrame(X_pp, columns=internal_names)

    explainer = shap.TreeExplainer(regressor)
    shap_values = explainer.shap_values(X_pp_arr, check_additivity=False)
    # Algunos modelos devuelven (n, n_features), otros lista por output.
    if isinstance(shap_values, list):
        shap_values = shap_values[0]
    return np.asarray(shap_values, dtype=float), internal_names


def _make_beeswarm_png(
    shap_by_raw: np.ndarray,
    raw_features: Sequence[str],
    df_ordered: pd.DataFrame,
    top_n: int = 15,
) -> Optional[str]:
    """Genera un beeswarm horizontal de las top_n raw features y lo devuelve
    como PNG codificado base64. None si matplotlib falla.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None

    top_features = df_ordered.head(top_n)["feature"].tolist()
    feat_idx = {f: i for i, f in enumerate(raw_features)}

    fig_height = max(4.0, 0.34 * len(top_features) + 1.0)
    fig, ax = plt.subplots(figsize=(8.5, fig_height), dpi=110)

    # Para cada feature top (de mas a menos importante en eje Y), ploteamos
    # los SHAP values de todas las filas como puntos con jitter y color.
    rng = np.random.default_rng(42)
    y_positions = np.arange(len(top_features))
    for y, feat in zip(y_positions[::-1], top_features):  # invertido: top arriba
        if feat not in feat_idx:
            continue
        col = feat_idx[feat]
        vals = shap_by_raw[:, col]
        if len(vals) == 0:
            continue
        # Color: map SHAP value sign to red/blue (rojo=empuja arriba, azul=abajo)
        norm_vals = vals / (np.max(np.abs(vals)) or 1.0)
        jitter = rng.uniform(-0.18, 0.18, size=len(vals))
        ax.scatter(
            vals,
            np.full_like(vals, y, dtype=float) + jitter,
            c=norm_vals,
            cmap="coolwarm",
            s=12,
            alpha=0.55,
            edgecolors="none",
            vmin=-1.0, vmax=1.0,
        )

    ax.set_yticks(y_positions[::-1])
    ax.set_yticklabels(top_features, fontsize=10)
    ax.axvline(0, color="#9ca3af", linestyle="--", linewidth=0.8)
    ax.set_xlabel("SHAP value (impacto en la prediccion)", fontsize=10)
    ax.set_title(
        f"Distribucion de impacto SHAP por variable (top {len(top_features)})",
        fontsize=11, loc="left",
    )
    ax.grid(axis="x", linestyle=":", alpha=0.4)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------


def compute_feature_importance(
    pipeline,
    X: pd.DataFrame,
    y: pd.Series,
    *,
    n_repeats: int = 10,         # ignorado (compat permutation API), SHAP no lo usa
    random_state: int = 42,
    scoring: str = "shap_tree",  # ignorado; SHAP siempre usa TreeExplainer
    n_jobs: int = -1,            # ignorado; SHAP TreeExplainer es single-thread interno
    sample_weight=None,           # ignorado; SHAP no usa sample weights
    n_sample: int = 2000,
    make_beeswarm: bool = True,
) -> FeatureImportanceResult:
    """Corre SHAP TreeExplainer sobre el champion + clasifica cada raw feature.

    Args
    ----
    pipeline      : Champion ya entrenado. Puede ser StackedRegressor,
                    OOFEnsembleRegressor o Pipeline directo.
    X, y          : dataset RAW (40 cols pre-preprocessing).
    n_sample      : si len(X) > n_sample, submuestra n_sample filas para
                    SHAP. Default 2000 = 20% del dataset, suficiente para
                    una estimacion global estable en ~30-90s.
    make_beeswarm : genera PNG base64 del beeswarm para embeber en HTML.

    Returns
    -------
    FeatureImportanceResult con df de raw features clasificadas.
    """
    # 1. MAE base sobre dataset COMPLETO (no submuestra) para fidelidad.
    y_pred = pipeline.predict(X)
    mae_base = float(np.mean(np.abs(np.asarray(y_pred) - np.asarray(y))))

    # 2. Submuestra para SHAP si el dataset es grande.
    if n_sample > 0 and len(X) > n_sample:
        rng = np.random.default_rng(random_state)
        idx = rng.choice(len(X), size=n_sample, replace=False)
        X_shap = X.iloc[idx].reset_index(drop=True)
        n_used = n_sample
    else:
        X_shap = X
        n_used = len(X)

    # 3. Extrae los K (preprocessor, regressor) del ensemble.
    estimators = _extract_estimators(pipeline)
    n_models = len(estimators)

    logger.info(
        f"SHAP feature importance | n_features={X.shape[1]} | n_rows={len(X)} | "
        f"n_sample={n_used} | n_models={n_models} | MAE_base={mae_base:.4f}"
    )

    # 4. Para cada modelo, computa SHAP. Promedia al final.
    all_shap: List[np.ndarray] = []
    internal_names: Optional[List[str]] = None
    for i, (pp, reg) in enumerate(estimators):
        try:
            shap_vals, names = _compute_shap_for_one(pp, reg, X_shap)
        except Exception as exc:
            logger.warning(f"SHAP fallo en modelo {i+1}/{n_models}: {exc}")
            continue
        if internal_names is None:
            internal_names = names
        elif names != internal_names:
            # Si la K-esima da nombres distintos (raro: clones del mismo pipeline
            # deben emitir idem), nos quedamos con los del primero. Esto NO
            # corrompe el average si las posiciones coinciden -- defensive.
            logger.warning(f"Modelo {i+1} emite columnas distintas; usando posiciones")
        all_shap.append(shap_vals)

    if not all_shap:
        raise RuntimeError(
            "No se pudo calcular SHAP para ningun modelo del ensemble."
        )

    avg_shap = np.mean(np.stack(all_shap, axis=0), axis=0)  # (n_sample, n_internal)

    # 5. Mapeo internal -> raw + agregacion.
    raw_columns = list(X.columns)
    mapping = _build_internal_to_raw_map(internal_names, raw_columns)

    # Construimos shap_by_raw (n_sample, n_raw) sumando contribuciones por grupo.
    raw_to_idx = {raw: i for i, raw in enumerate(raw_columns)}
    n_rows = avg_shap.shape[0]
    shap_by_raw = np.zeros((n_rows, len(raw_columns)), dtype=float)
    for i_internal, internal in enumerate(internal_names):
        raw = mapping.get(internal, internal)
        if raw not in raw_to_idx:
            # Feature interna que no mapea a ninguna raw conocida (e.g.,
            # __ISNAN de cols sin raw equivalente). La omitimos: no pertenece
            # a ninguna decision de poda sobre raw.
            continue
        shap_by_raw[:, raw_to_idx[raw]] += avg_shap[:, i_internal]

    # 6. Construye el DataFrame final.
    importance_mean = np.mean(np.abs(shap_by_raw), axis=0)
    importance_std = np.std(np.abs(shap_by_raw), axis=0)
    direction_mean = np.mean(shap_by_raw, axis=0)

    df = pd.DataFrame(
        {
            "feature": raw_columns,
            "importance_mean": importance_mean,
            "importance_std": importance_std,
            "direction_mean": direction_mean,
        }
    ).sort_values("importance_mean", ascending=False).reset_index(drop=True)
    df.insert(0, "rank", df.index + 1)

    total_imp = float(df["importance_mean"].sum()) or 1.0
    df["share"] = df["importance_mean"] / total_imp
    df["status"] = df["share"].apply(_classify)

    # 7. Beeswarm PNG (opcional).
    beeswarm_b64: Optional[str] = None
    if make_beeswarm:
        try:
            beeswarm_b64 = _make_beeswarm_png(shap_by_raw, raw_columns, df)
        except Exception as exc:
            logger.warning(f"Beeswarm PNG fallo: {exc}")

    out = FeatureImportanceResult(
        df=df,
        n_samples=n_used,
        n_models=n_models,
        method="shap_tree",
        mae_base=mae_base,
        beeswarm_b64=beeswarm_b64,
        shap_by_raw=shap_by_raw,
        raw_features=raw_columns,
    )

    summary = out.to_dict_summary()
    logger.info(
        f"SHAP done | core={summary['fi_n_core']} util={summary['fi_n_util']} "
        f"podable={summary['fi_n_prunable']} ruido={summary['fi_n_noise']} | "
        f"top1='{summary['fi_top1_feature']}' "
        f"({summary['fi_top1_importance']:.4f})"
    )
    return out

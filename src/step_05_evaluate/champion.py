"""Selecciona el modelo CAMPEON entre varios entrenados para la misma variedad.

Cada modelo (XGB, LGB, ...) entrena de forma INDEPENDIENTE: su propio Optuna
study, su propio search space, su propio MLflow run. Cuando todos terminan,
comparamos sus metricas con un criterio LEX-ORDER (prioridad estricta) que
refleja el contrato de MLOps:

    1. GENERALIZACION: menor brecha (|gap|) entre Train y Test (evita overfit).
    2. RENDIMIENTO TOTAL: menor MAPE de negocio sobre el dataset completo
       (refit + predict all). Es el "sanity check" del modelo de produccion.
    3. EFICIENCIA: menor tiempo de entrenamiento ante empate practico.

Para empates "blandos" en gap usamos una banda de tolerancia
(`GAP_TIE_TOLERANCE`): si dos modelos difieren por menos de ese delta, los
consideramos equivalentes en gap y pasamos al siguiente criterio. Sin esa
tolerancia el orden seria inestable frente a ruido de CV.

Adicionalmente exponemos `composite_score` (legacy) que combina MAPE de
negocio + penalizacion de gap. Lo dejamos como metrica auxiliar para logs y
MLflow tags, pero el campeon NO lo usa para la decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

import numpy as np

if TYPE_CHECKING:
    from src.step_06_track.business_validation import BusinessValidation


# Tolerancia "practica" sobre el |gap|. Dos modelos cuyo |gap| difiere en
# menos de esto se consideran empate en estabilidad y se desempata por el
# siguiente criterio. 0.5 pp del KG/JR_H suele estar dentro del ruido de CV
# para datasets de 1k-10k filas.
GAP_TIE_TOLERANCE: float = 0.005

# Tolerancia sobre MAPE total (en %). Empate de rendimiento -> desempata por
# tiempo. 0.5 pp de MAPE es ruido tipico entre seeds distintas.
FULL_MAPE_TIE_TOLERANCE: float = 0.5


@dataclass
class ModelResult:
    """Resultado de entrenar UN modelo para UNA variedad.

    Campos clave para la decision (poblados por `single_run`):
      - metrics['nested_cv_gap_mean'] : Train-Test gap (overfitting).
      - full_metrics['mape']          : MAPE en KG/JR sobre dataset COMPLETO
                                        (refit + predict all). Estabilidad.
      - elapsed_seconds               : tiempo total de entrenamiento.

    Campos enriquecidos para los renderers (poblados por `single_run` justo
    despues del fit y consumidos UNA VEZ en `variety_runner` para construir
    el dashboard / Excel del campeon):
      - business_validation : BusinessValidation con OOF/in-sample en KG/JR.
      - full_metrics_h      : metricas in-sample en KG/JR_H (unidad modelo).
      - oof_y_true/pred     : arrays OOF en KG/JR_H (alineados con X).

    Estos campos pueden ser None cuando el modelo se reconstruye desde
    `variety_summary_*.json` (sin reabrir el pipeline). Los renderers
    deben tolerar None.
    """

    model_type: str
    metrics: Dict[str, float]
    best_params: Dict[str, object]
    mlflow_run_id: str
    pipeline_path: str
    elapsed_seconds: float
    business_metrics_oof: Optional[Dict[str, float]] = None
    full_metrics: Optional[Dict[str, float]] = None  # KG/JR aplicado a TODO X
    business_validation: Optional["BusinessValidation"] = None
    full_metrics_h: Optional[Dict[str, float]] = None  # KG/JR_H in-sample
    oof_y_true: Optional[np.ndarray] = None
    oof_y_pred: Optional[np.ndarray] = None
    composite_score: float = field(init=False)

    def __post_init__(self) -> None:
        self.composite_score = composite_score(self.metrics, self.business_metrics_oof)

    @property
    def abs_gap(self) -> float:
        return abs(float(self.metrics.get("nested_cv_gap_mean", 0.0)))

    @property
    def full_mape(self) -> float:
        """MAPE en KG/JR sobre el dataset completo (mas bajo es mejor).

        Si no hay metricas full disponibles, cae al MAPE OOF de negocio.
        Si tampoco, devuelve infinito (deja al modelo en ultimo lugar).
        """
        if self.full_metrics and "mape" in self.full_metrics:
            return float(self.full_metrics["mape"])
        if self.business_metrics_oof and "mape" in self.business_metrics_oof:
            return float(self.business_metrics_oof["mape"])
        return float("inf")


def composite_score(
    metrics: Dict[str, float],
    business_metrics_oof: Optional[Dict[str, float]] = None,
    gap_weight: float = 0.05,
) -> float:
    """[LEGACY] Score auxiliar 'menor es mejor'. NO usar en codigo nuevo.

    Lo conservamos porque MLflow ya lo loguea como tag y los dashboards
    historicos lo leen. La decision del campeon usa lex-order en
    `select_champion`, NO este score.

    Single point of compute: solo se invoca desde `ModelResult.__post_init__`.
    El resto del codigo lee `r.composite_score` (atributo cacheado), nunca
    re-llama esta funcion.
    """
    gap = max(0.0, float(metrics.get("nested_cv_gap_mean", 0.0)))

    if business_metrics_oof and "mape" in business_metrics_oof:
        mape = float(business_metrics_oof["mape"])
        return mape + gap_weight * gap

    mae = float(metrics.get("nested_cv_mae_mean", float("inf")))
    return mae + 0.5 * gap


def _decision_key(r: "ModelResult") -> tuple:
    """Llave lex-order para `min(...)`. Aplica las tolerancias por bucket.

    Bucket por gap: redondeamos a multiplos de GAP_TIE_TOLERANCE para que
    diferencias por debajo del ruido caigan en el mismo bucket. Idem MAPE.
    Cuando dos modelos comparten bucket, el siguiente criterio decide.
    """
    gap_bucket = round(r.abs_gap / GAP_TIE_TOLERANCE)
    mape_bucket = round(r.full_mape / FULL_MAPE_TIE_TOLERANCE)
    return (gap_bucket, mape_bucket, r.elapsed_seconds)


def select_champion(results: List[ModelResult]) -> ModelResult:
    """Devuelve el ganador segun lex-order (gap -> full -> tiempo).

    Levanta ValueError si la lista esta vacia.
    """
    if not results:
        raise ValueError("select_champion: lista de results vacia")
    return min(results, key=_decision_key)


def _justification(
    champion: ModelResult,
    rivals: List[ModelResult],
) -> str:
    """Texto humano explicando por que `champion` gano sobre los rivales.

    Generado dinamicamente comparando los tres ejes de decision.
    """
    if not rivals:
        return (
            f"{champion.model_type.upper()} fue el unico modelo entrenado para "
            f"esta variedad: gap={champion.abs_gap:.4f}, "
            f"MAPE_total={champion.full_mape:.2f}%, "
            f"tiempo={champion.elapsed_seconds:.1f}s."
        )

    lines: List[str] = []
    for rival in rivals:
        d_gap = rival.abs_gap - champion.abs_gap
        d_mape = rival.full_mape - champion.full_mape
        d_time = rival.elapsed_seconds - champion.elapsed_seconds

        if d_gap > GAP_TIE_TOLERANCE:
            lines.append(
                f"{rival.model_type.upper()} descartado por overfitting: "
                f"|gap|={rival.abs_gap:.4f} vs {champion.abs_gap:.4f} del campeon "
                f"({d_gap:+.4f} de mas)."
            )
        elif d_mape > FULL_MAPE_TIE_TOLERANCE:
            lines.append(
                f"{rival.model_type.upper()} descartado por menor estabilidad "
                f"en data total: MAPE={rival.full_mape:.2f}% vs "
                f"{champion.full_mape:.2f}% del campeon ({d_mape:+.2f} pp)."
            )
        elif d_time > 0:
            lines.append(
                f"{rival.model_type.upper()} descartado por eficiencia "
                f"(empate tecnico en gap y MAPE): tiempo={rival.elapsed_seconds:.1f}s "
                f"vs {champion.elapsed_seconds:.1f}s ({d_time:+.1f}s mas)."
            )
        else:
            lines.append(
                f"{rival.model_type.upper()} empata tecnicamente con el campeon; "
                f"se elige {champion.model_type.upper()} por orden estable."
            )
    return " ".join(lines)


def champion_summary(
    results: List[ModelResult],
    champion: ModelResult,
) -> Dict[str, object]:
    """Diccionario serializable describiendo la decision (para JSON / dashboard).

    Incluye el ranking completo, las metricas relevantes por modelo y un
    bloque de justificacion textual auto-generado.
    """
    ranking = sorted(results, key=_decision_key)
    rivals = [r for r in ranking if r.model_type != champion.model_type]
    return {
        "champion_model": champion.model_type,
        "champion_run_id": champion.mlflow_run_id,
        "champion_composite_score": champion.composite_score,
        "decision_criteria": [
            "1_min_abs_gap (overfitting)",
            "2_min_full_mape (estabilidad data total)",
            "3_min_elapsed_seconds (eficiencia)",
        ],
        "tolerances": {
            "gap": GAP_TIE_TOLERANCE,
            "full_mape_pp": FULL_MAPE_TIE_TOLERANCE,
        },
        "justification": _justification(champion, rivals),
        "ranking": [
            {
                "model": r.model_type,
                "rank": i + 1,
                "is_champion": r.model_type == champion.model_type,
                "abs_gap": r.abs_gap,
                "full_mape": r.full_mape if r.full_mape != float("inf") else None,
                "elapsed_seconds": r.elapsed_seconds,
                "composite": r.composite_score,
                "mae_test": r.metrics.get("nested_cv_mae_mean"),
                "mae_train": r.metrics.get("nested_cv_mae_train_mean"),
                "gap": r.metrics.get("nested_cv_gap_mean"),
                "r2": r.metrics.get("nested_cv_r2_mean"),
                "business_mape_oof": (
                    r.business_metrics_oof.get("mape")
                    if r.business_metrics_oof else None
                ),
                "business_r2_oof": (
                    r.business_metrics_oof.get("r2")
                    if r.business_metrics_oof else None
                ),
                "full_r2": (r.full_metrics or {}).get("r2"),
                "full_mae": (r.full_metrics or {}).get("mae"),
            }
            for i, r in enumerate(ranking)
        ],
    }

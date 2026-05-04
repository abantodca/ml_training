"""Diagnóstico de la capa meta (stacking) listo para presentar.

Centraliza el duck-typing sobre `StackedRegressor` para que tanto
`single_run.py` (logging MLflow) como `winner_dashboard.py` (HTML) y
`business_export.py` (Excel) consuman el mismo dataclass sin reimplementar
la extracción.

Razón de diseño: `step_05_evaluate` no debe importar `step_04_train.stacking`
directamente — eso acoplaría la presentación al detalle de implementación
del wrapper. Aquí hacemos duck typing por nombres de atributo (`_decision_log_`,
`meta_feature_names_`, etc.). Si el pipeline no es un StackedRegressor el
extractor devuelve None silenciosamente.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np


# Margen relativo (en %) bajo el cual el meta se considera "no aporta" y el
# auto-fallback lo desactiva. Mantener sincronizado con
# `stacking._FALLBACK_RELATIVE_MARGIN` (0.005 == 0.5%). Lo duplicamos como
# float visible para que el HTML pueda mostrarlo sin importar stacking.py.
_FALLBACK_THRESHOLD_PCT: float = 0.5


@dataclass(frozen=True)
class StackingDiagnostics:
    """Resumen presentable de la capa meta sobre el campeón.

    Se extrae con duck-typing del `final_pipeline`. Si el pipeline NO es
    un StackedRegressor, el extractor devuelve None (no hay stacking).

    Attributes
    ----------
    meta_type      : 'gam' por ahora; reservado para futuros backends meta.
    active         : True si `predict()` usa la cascada base→meta. False si
                     el auto-fallback desactivó el meta y predict() == base.
    mae_base_oof   : MAE del base puro sobre las OOF que vio el meta (KG/JR_H).
    mae_meta_oof   : MAE del meta sobre las mismas OOF (KG/JR_H).
    delta_pct      : (mae_meta - mae_base) / mae_base * 100.
                     Negativo = el meta mejora al base. Positivo = empeora.
    n_features     : número total de columnas que vio el GAM.
    feature_names  : columnas del GAM (col 0 = pred_base + x_subset + flags).
    cat_features   : nombres categóricos (subset de feature_names).
    nan_flag_features: cols __ISNAN auto-generadas.
    tuned_params   : (gam_n_splines, gam_lam) finales — tuneados o defaults.
    tuned          : True si hubo Optuna sobre el meta y NO cayó al fallback.
    tuning_log     : trials, inner_folds, best_score_mae, elapsed_s, fallback_used.
    fallback_threshold_pct : margen para activar el fallback (en %).
    """

    meta_type: str = "gam"
    active: bool = True
    mae_base_oof: float = float("nan")
    mae_meta_oof: float = float("nan")
    delta_pct: float = float("nan")
    n_features: int = 0
    feature_names: List[str] = field(default_factory=list)
    cat_features: List[str] = field(default_factory=list)
    nan_flag_features: List[str] = field(default_factory=list)
    tuned_params: Dict[str, float] = field(default_factory=dict)
    tuned: bool = False
    tuning_log: Dict[str, float] = field(default_factory=dict)
    fallback_threshold_pct: float = _FALLBACK_THRESHOLD_PCT

    @property
    def improves_base(self) -> bool:
        """True si el meta efectivamente mejora al base (delta < 0)."""
        return bool(np.isfinite(self.delta_pct) and self.delta_pct < 0)

    @property
    def status_label(self) -> str:
        """Etiqueta corta para el hero / pill."""
        if not self.active:
            return "FALLBACK"
        return "ACTIVA"

    @property
    def headline(self) -> str:
        """Frase ejecutiva — la conclusión en una línea para gerencia."""
        if not self.active:
            return (
                "La capa meta no superó al modelo base, así que el sistema "
                "decidió usar solo el modelo base. Activar el stacking nunca "
                "empeora el resultado."
            )
        if self.improves_base:
            return (
                f"La capa meta refina la predicción del modelo base y reduce "
                f"el error en {abs(self.delta_pct):.2f}% (medido en datos "
                "que el modelo NO vio durante el entrenamiento)."
            )
        # Activa pero no mejora → caso raro (fallback desactivado por config)
        return (
            f"La capa meta está activa con un cambio de {self.delta_pct:+.2f}% "
            "en el error. El auto-fallback está deshabilitado por configuración."
        )

    @property
    def technical_line(self) -> str:
        """Una línea con números crudos para la sección técnica."""
        return (
            f"MAE base OOF = {self.mae_base_oof:.4f} · "
            f"MAE meta OOF = {self.mae_meta_oof:.4f} · "
            f"Δ = {self.delta_pct:+.2f}% · "
            f"meta_active = {self.active}"
        )


def extract_stacking_diagnostics(final_pipeline: Any) -> Optional[StackingDiagnostics]:
    """Devuelve diagnóstico si `final_pipeline` es un StackedRegressor.

    Duck typing: detectamos `_decision_log_` + `meta_feature_names_` (los
    dos atributos clave que sólo `StackedRegressor` tiene tras fit). Si
    falta cualquiera, devolvemos None — el pipeline no usa stacking o el
    fit aún no terminó.
    """
    if (
        not hasattr(final_pipeline, "_decision_log_")
        or not hasattr(final_pipeline, "meta_feature_names_")
    ):
        return None

    log = getattr(final_pipeline, "_decision_log_", {}) or {}
    feature_names = list(getattr(final_pipeline, "meta_feature_names_", []) or [])
    cat_indices = list(getattr(final_pipeline, "cat_indices_", []) or [])
    cat_features = [
        feature_names[i] for i in cat_indices if 0 <= i < len(feature_names)
    ]
    nan_flag_features = [n for n in feature_names if n.endswith("__ISNAN")]

    tuned_params_raw = getattr(final_pipeline, "tuned_meta_params_", None) or {}
    tuned_params: Dict[str, float] = {}
    for k, v in tuned_params_raw.items():
        try:
            tuned_params[str(k)] = float(v)
        except (TypeError, ValueError):
            continue

    tuning_log_raw = getattr(final_pipeline, "tuning_log_", None) or {}
    tuning_log: Dict[str, float] = {}
    for k, v in tuning_log_raw.items():
        if isinstance(v, (int, float)) and not (isinstance(v, float) and np.isnan(v)):
            tuning_log[str(k)] = float(v)
    # tuned = corrió tuning (n_trials > 0) y NO cayó a defaults por excepción.
    tuned = bool(
        tuning_log_raw
        and tuning_log_raw.get("n_trials", 0) > 0
        and not tuning_log_raw.get("fallback_used", False)
    )

    active = bool(getattr(final_pipeline, "_meta_active_", True))

    return StackingDiagnostics(
        meta_type="gam",
        active=active,
        mae_base_oof=float(log.get("mae_base_oof", float("nan"))),
        mae_meta_oof=float(log.get("mae_meta_oof", float("nan"))),
        delta_pct=float(log.get("delta_pct", float("nan"))),
        n_features=len(feature_names),
        feature_names=feature_names,
        cat_features=cat_features,
        nan_flag_features=nan_flag_features,
        tuned_params=tuned_params,
        tuned=tuned,
        tuning_log=tuning_log,
        fallback_threshold_pct=_FALLBACK_THRESHOLD_PCT,
    )


__all__ = ["StackingDiagnostics", "extract_stacking_diagnostics"]

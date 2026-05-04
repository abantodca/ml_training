"""Traduccion de metricas tecnicas a lenguaje ejecutivo.

Centraliza toda la logica de "como contar el resultado a un no-tecnico":
veredicto del modelo, KPIs en lenguaje natural, comparacion contra
baseline ingenuo, deteccion de subgrupos problematicos, glosario y
contexto del entrenamiento. Lo consumen tanto `winner_dashboard.py`
(HTML) como `business_export.py` (Excel) para que ambos digan lo mismo.

`build_winner_kit(...)` es la unica fabrica de inputs ejecutivos: arma de
una sola pasada (real, pred, abs_err, X_aligned, oof_mape/r2, verdict,
context, kpis, actions) desde un `ModelResult` campeon. HTML y Excel
consumen el mismo kit -> nunca se desincronizan.

Convencion: ningun string aqui adentro deberia contener jerga (MAPE, R2,
gap, OOF) sin traducirla. Si necesitas algo tecnico, llamalo desde su
funcion auxiliar y deja la version ejecutiva como output principal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.config import (
    ABS_GAP_WARN,
    FULL_MAPE_CRITICAL_PCT,
    KPI_BASELINE_HIGH_IMPROVEMENT_PCT,
    KPI_BASELINE_MEDIUM_IMPROVEMENT_PCT,
    KPI_PRECISION_HIGH_MAPE_PCT,
    KPI_PRECISION_MEDIUM_MAPE_PCT,
    KPI_R2_HIGH_PCT,
    KPI_R2_MEDIUM_PCT,
    REPORT_SUBGROUP_MIN_N,
    REPORT_SUBGROUP_WARN_RATIO,
    REPORT_VERDICT_THRESHOLDS,
)

from src.step_05_evaluate.stacking_diagnostics import StackingDiagnostics

if TYPE_CHECKING:
    from src.step_05_evaluate.champion import ModelResult
    from src.step_05_evaluate.feature_importance import FeatureImportanceResult


# -----------------------------------------------------------------------------
# Veredicto ejecutivo
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """Veredicto del modelo en formato presentable.

    level     : 'alta_confianza' | 'confianza_aceptable' | 'confianza_limitada' | 'no_recomendado'
    icon      : emoji para badge (✅ / 🟢 / ⚠ / 🚫)
    title     : titulo corto para el hero (e.g. "Listo para producción")
    headline  : 1 frase de cabecera (e.g. "Modelo apto para uso operacional")
    body      : 1-2 frases con la recomendacion accionable
    color_key : 'green' | 'green-2' | 'amber' | 'red' (para CSS class)
    """

    level: str
    icon: str
    title: str
    headline: str
    body: str
    color_key: str


_VERDICTS: Dict[str, Verdict] = {
    "alta_confianza": Verdict(
        level="alta_confianza",
        icon="✅",
        title="Listo para producción",
        headline="Modelo apto para integrarse al flujo operacional.",
        body=(
            "Los errores son consistentes y la diferencia entre lo que el "
            "modelo aprendió y lo que predice en datos nuevos es mínima. "
            "Recomendamos desplegarlo y monitorear mensualmente."
        ),
        color_key="green",
    ),
    "confianza_aceptable": Verdict(
        level="confianza_aceptable",
        icon="🟢",
        title="Apto con monitoreo",
        headline="Modelo útil para producción con seguimiento cercano.",
        body=(
            "Los errores están dentro de límites aceptables. Recomendamos "
            "usarlo como apoyo a las decisiones operativas y revisar las "
            "métricas mensualmente para detectar deterioro."
        ),
        color_key="green-2",
    ),
    "confianza_limitada": Verdict(
        level="confianza_limitada",
        icon="⚠",
        title="Usar como referencia",
        headline="Modelo informativo, no como autoridad operativa.",
        body=(
            "El error es manejable pero hay señales de inconsistencia "
            "entre lo aprendido y lo predicho. Combinar siempre con "
            "criterio humano hasta acumular más datos o reentrenar."
        ),
        color_key="amber",
    ),
    "no_recomendado": Verdict(
        level="no_recomendado",
        icon="🚫",
        title="No recomendado para producción",
        headline="Modelo con variabilidad alta — investigar antes de usar.",
        body=(
            "El error es elevado o la diferencia entre entrenamiento y "
            "predicción real es grande. Recomendamos NO desplegar hasta "
            "diagnosticar la causa (datos, features, segmentación)."
        ),
        color_key="red",
    ),
}


def compute_verdict(*, full_mape_pct: float, abs_gap: float) -> Verdict:
    """Devuelve el veredicto ejecutivo segun los umbrales de config.

    El modelo cae en el nivel MAS CONSERVADOR donde ambas metricas entren.
    Es decir, basta que UNA de las dos viole el limite del nivel para
    bajar al siguiente. Los thresholds se leen de
    `REPORT_VERDICT_THRESHOLDS` para poder tunearlos sin tocar codigo.
    """
    levels_in_order = [
        "alta_confianza",
        "confianza_aceptable",
        "confianza_limitada",
    ]
    for level in levels_in_order:
        thr = REPORT_VERDICT_THRESHOLDS[level]
        if full_mape_pct <= thr["max_mape_pct"] and abs_gap <= thr["max_abs_gap"]:
            return _VERDICTS[level]
    return _VERDICTS["no_recomendado"]


# -----------------------------------------------------------------------------
# KPIs en lenguaje natural
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class PlainKPI:
    """KPI ejecutivo: titulo de la pregunta + respuesta en una frase."""

    question: str        # "¿Qué tan preciso es?"
    headline: str        # "8 de 10 cosechas con error ≤ 7 kg/jornal"
    detail: str          # "Mediana del error: 4.2 kg/jornal..."
    technical: str       # "MAPE OOF = 17.5% (KG/JR)"
    score_label: str     # "ALTO" | "MEDIO" | "BAJO" — semaforo opcional


def kpi_precision(abs_errors: np.ndarray, mape_pct: float) -> PlainKPI:
    """Pregunta 1: ¿Que tan preciso es?

    Reporta el percentil 80 del error absoluto en KG/JR ("8 de cada 10").
    Esa cifra es mas accionable que la mediana para gerencia: dice "el
    peor caso en 80% de las veces".
    """
    if abs_errors.size == 0:
        return PlainKPI(
            question="¿Qué tan preciso es?",
            headline="Sin datos suficientes para evaluar precisión",
            detail="No se pudo calcular el error en unidades de negocio.",
            technical="—",
            score_label="—",
        )

    p50 = float(np.percentile(abs_errors, 50))
    p80 = float(np.percentile(abs_errors, 80))

    score = (
        "ALTO" if mape_pct <= KPI_PRECISION_HIGH_MAPE_PCT
        else ("MEDIO" if mape_pct <= KPI_PRECISION_MEDIUM_MAPE_PCT else "BAJO")
    )

    return PlainKPI(
        question="¿Qué tan preciso es?",
        headline=f"8 de cada 10 predicciones tienen error ≤ {p80:.1f} kg/jornal",
        detail=(
            f"La mitad de las predicciones se equivoca en ≤ {p50:.1f} kg/jornal. "
            f"En promedio, cada predicción se desvía un {mape_pct:.1f}% del valor real."
        ),
        technical=f"MAPE OOF = {mape_pct:.2f}% · p50={p50:.2f} kg · p80={p80:.2f} kg",
        score_label=score,
    )


def kpi_explanatory_power(r2: Optional[float]) -> PlainKPI:
    """Pregunta 2: ¿Cuanto explica?

    Traduce R^2 a "captura el X% de la variabilidad observada". Una
    persona no tecnica entiende mejor "explicar variabilidad" que R^2.
    """
    if r2 is None or np.isnan(r2):
        return PlainKPI(
            question="¿Cuánto explica?",
            headline="Sin información de varianza explicada",
            detail="No se pudo calcular el coeficiente de determinación.",
            technical="—",
            score_label="—",
        )
    pct = float(r2) * 100.0
    score = (
        "ALTO" if pct >= KPI_R2_HIGH_PCT
        else ("MEDIO" if pct >= KPI_R2_MEDIUM_PCT else "BAJO")
    )
    return PlainKPI(
        question="¿Cuánto explica?",
        headline=f"Captura el {pct:.0f}% de la variabilidad observada",
        detail=(
            "Un modelo perfecto explicaría el 100%; uno que solo predice el "
            f"promedio explicaría 0%. Este modelo explica {pct:.0f}% del "
            "comportamiento real de la productividad."
        ),
        technical=f"R² OOF = {r2:.4f}",
        score_label=score,
    )


def kpi_vs_baseline(
    y_true: np.ndarray,
    y_pred: np.ndarray,
) -> PlainKPI:
    """Pregunta 3: ¿Vale la pena vs no usar modelo?

    Baseline ingenuo = predecir siempre la media de y_true. Calcula la
    mejora porcentual del MAE del modelo sobre el MAE del baseline.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    if y_true.size == 0:
        return PlainKPI(
            question="¿Vale la pena vs no usar modelo?",
            headline="Sin datos suficientes para comparar",
            detail="—",
            technical="—",
            score_label="—",
        )

    mae_model = float(np.mean(np.abs(y_pred - y_true)))
    baseline_pred = float(np.mean(y_true))
    mae_baseline = float(np.mean(np.abs(y_true - baseline_pred)))

    if mae_baseline <= 0:
        improvement_pct = 0.0
    else:
        improvement_pct = (mae_baseline - mae_model) / mae_baseline * 100.0

    if improvement_pct >= KPI_BASELINE_HIGH_IMPROVEMENT_PCT:
        score = "ALTO"
    elif improvement_pct >= KPI_BASELINE_MEDIUM_IMPROVEMENT_PCT:
        score = "MEDIO"
    else:
        score = "BAJO"

    return PlainKPI(
        question="¿Vale la pena vs no usar modelo?",
        headline=(
            f"{improvement_pct:.0f}% más preciso que predecir el promedio"
            if improvement_pct > 0
            else "Sin ventaja clara sobre predecir el promedio"
        ),
        detail=(
            f"Sin modelo, alguien que estimara siempre el promedio "
            f"({baseline_pred:.1f} kg/jornal) se equivocaría en promedio "
            f"{mae_baseline:.1f} kg. Con este modelo, el error promedio baja a "
            f"{mae_model:.1f} kg."
        ),
        technical=(
            f"MAE modelo={mae_model:.2f} · MAE baseline (media)={mae_baseline:.2f} · "
            f"mejora={improvement_pct:+.1f}%"
        ),
        score_label=score,
    )


# -----------------------------------------------------------------------------
# Acciones recomendadas
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """Recomendacion accionable auto-generada."""

    severity: str   # 'critical' | 'warning' | 'info'
    icon: str
    title: str
    body: str


def recommended_actions(
    *,
    abs_errors: np.ndarray,
    real: np.ndarray,
    X_aligned: Optional[pd.DataFrame],
    global_mape: float,
    abs_gap: float,
    full_mape: float,
) -> List[Action]:
    """Genera 0-5 acciones a partir de subgroups y metricas globales.

    Reglas (umbrales en config.py):
      - Si un FORMATO/FUNDO tiene MAPE >= REPORT_SUBGROUP_WARN_RATIO * global -> warning.
      - Si abs_gap > ABS_GAP_WARN -> warning de overfitting en lenguaje natural.
      - Si full_mape > FULL_MAPE_CRITICAL_PCT -> critical.
      - Si nada de arriba aplica -> info "todo OK".
    """
    actions: List[Action] = []

    # Globales
    if full_mape > FULL_MAPE_CRITICAL_PCT:
        actions.append(Action(
            severity="critical",
            icon="🚫",
            title="Error global alto",
            body=(
                f"El error promedio del modelo ({full_mape:.1f}%) excede "
                "el umbral aceptable. No usar predicciones para decisiones "
                "operativas críticas hasta diagnosticar la causa."
            ),
        ))
    if abs_gap > ABS_GAP_WARN:
        actions.append(Action(
            severity="warning",
            icon="⚠",
            title="El modelo memorizó parte del entrenamiento",
            body=(
                f"Hay una diferencia notable entre el error en datos vistos "
                f"y datos nuevos (brecha = {abs_gap:.3f}). Esto suele "
                "indicar que el modelo aprendió patrones específicos del "
                "histórico que pueden no repetirse. Considerar reducir "
                "complejidad o agregar más datos antes de desplegar."
            ),
        ))

    # Subgrupos
    if X_aligned is not None and len(X_aligned) == abs_errors.size and global_mape > 0:
        warn_thr = global_mape * REPORT_SUBGROUP_WARN_RATIO
        for col in ("FORMATO", "FUNDO"):
            if col not in X_aligned.columns:
                continue
            groups = X_aligned[col].astype(str).reset_index(drop=True)
            for cat in groups.unique():
                if pd.isna(cat) or cat == "":
                    continue
                mask = (groups == cat).to_numpy()
                if mask.sum() < REPORT_SUBGROUP_MIN_N:  # ignora subgrupos diminutos
                    continue
                cat_real = real[mask]
                cat_err = abs_errors[mask]
                nonzero = cat_real != 0
                if nonzero.sum() == 0:
                    continue
                cat_mape = float(np.mean(cat_err[nonzero] / np.abs(cat_real[nonzero])) * 100)
                if cat_mape >= warn_thr:
                    ratio = cat_mape / global_mape if global_mape > 0 else float("inf")
                    actions.append(Action(
                        severity="warning",
                        icon="⚠",
                        title=f"{col} '{cat}': error {ratio:.1f}× mayor que el promedio",
                        body=(
                            f"En este segmento ({int(mask.sum())} cosechas) el error "
                            f"medio es {cat_mape:.1f}% vs {global_mape:.1f}% del global. "
                            "Recomendamos NO automatizar predicciones aquí — usar "
                            "criterio operativo o entrenar modelo dedicado."
                        ),
                    ))

    if not actions:
        actions.append(Action(
            severity="info",
            icon="✅",
            title="No se detectaron problemas significativos",
            body=(
                "Las métricas globales y por subgrupo están dentro de rangos "
                "esperados. Continuar con el plan de despliegue y monitoreo."
            ),
        ))

    return actions


# -----------------------------------------------------------------------------
# Contexto del entrenamiento
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainingContext:
    """Datos descriptivos del dataset para mostrar al lector."""

    variety: str
    n_rows: int
    n_features: int
    date_min: Optional[str]
    date_max: Optional[str]
    n_fundos: int
    fundos_top: List[str]      # primeros 5 alfabeticamente
    n_formatos: int
    formatos_top: List[str]    # primeros 5 alfabeticamente


def build_context(
    variety: str,
    X_raw: Optional[pd.DataFrame],
    date_col: str = "FECHA",
) -> TrainingContext:
    """Extrae el contexto presentable desde el dataset original."""
    n_rows = int(len(X_raw)) if X_raw is not None else 0
    n_features = int(X_raw.shape[1]) if X_raw is not None else 0
    date_min = date_max = None
    n_fundos = n_formatos = 0
    fundos_top: List[str] = []
    formatos_top: List[str] = []

    if X_raw is not None:
        if date_col in X_raw.columns:
            try:
                d = pd.to_datetime(X_raw[date_col], errors="coerce").dropna()
                if not d.empty:
                    date_min = d.min().strftime("%Y-%m")
                    date_max = d.max().strftime("%Y-%m")
            except Exception:
                pass
        if "FUNDO" in X_raw.columns:
            uniq = sorted(X_raw["FUNDO"].dropna().astype(str).unique())
            n_fundos = len(uniq)
            fundos_top = uniq[:5]
        if "FORMATO" in X_raw.columns:
            uniq = sorted(X_raw["FORMATO"].dropna().astype(str).unique())
            n_formatos = len(uniq)
            formatos_top = uniq[:5]

    return TrainingContext(
        variety=variety,
        n_rows=n_rows,
        n_features=n_features,
        date_min=date_min,
        date_max=date_max,
        n_fundos=n_fundos,
        fundos_top=fundos_top,
        n_formatos=n_formatos,
        formatos_top=formatos_top,
    )


# -----------------------------------------------------------------------------
# Glosario
# -----------------------------------------------------------------------------

# Diccionario unificado de terminos tecnicos -> definicion para el lector
# no-tecnico. Se renderiza tanto en el HTML (tooltips + tabla) como en el
# Excel (hoja "Glosario"). Mantener corto y claro.
GLOSSARY: Dict[str, str] = {
    "MAPE": (
        "Error porcentual promedio. Por ejemplo, MAPE=15% significa que en "
        "promedio cada predicción se desvía un 15% del valor real."
    ),
    "MAE": (
        "Error absoluto promedio en las unidades originales (kg/jornal). "
        "Por ejemplo, MAE=5 significa que el modelo se equivoca en "
        "promedio en 5 kg por jornal."
    ),
    "R² (R cuadrado)": (
        "Porcentaje de la variabilidad de los datos que el modelo logra "
        "explicar. Va de 0 a 100%: 100% = modelo perfecto, 0% = el modelo "
        "no aporta nada vs predecir el promedio."
    ),
    "OOF (Out-Of-Fold)": (
        "Métrica calculada con predicciones que el modelo NUNCA vio "
        "durante su entrenamiento. Es la forma honesta de medir error: "
        "lo que esperamos en producción real."
    ),
    "In-sample / Aplicación Total": (
        "Métrica calculada cuando el modelo predice los mismos datos con "
        "los que se entrenó. Es OPTIMISTA — sirve solo como sanity check."
    ),
    "Train (Entrenamiento)": (
        "Datos que el modelo ve durante el aprendizaje. El error en train "
        "siempre es bajo; lo que importa es cómo se comporta en datos "
        "nuevos (Test)."
    ),
    "Test (Prueba)": (
        "Datos que el modelo NO vio en entrenamiento, usados para medir "
        "qué tan bien generaliza a casos nuevos."
    ),
    "Brecha Train-Test (gap, overfitting)": (
        "Diferencia entre el error en datos vistos vs datos nuevos. "
        "Brecha grande = el modelo memorizó el pasado pero no generaliza. "
        "Brecha chica = aprendió patrones reales y reproducibles."
    ),
    "KG/JR_H": (
        "Kilogramos cosechados por jornal-hora. Es la unidad técnica que "
        "predice el modelo (elimina el efecto de la duración de la jornada)."
    ),
    "KG/JR": (
        "Kilogramos cosechados por jornal completo. Es la unidad de "
        "negocio: KG/JR = KG/JR_H × duración de la jornada."
    ),
    "Nested Cross-Validation": (
        "Protocolo de evaluación donde la búsqueda de hiperparámetros "
        "ocurre dentro de cada partición, sin filtrar información del "
        "test. Es la forma rigurosa de medir generalización."
    ),
    "Baseline": (
        "Predicción ingenua usada como referencia (e.g., predecir siempre "
        "el promedio). Si un modelo no es mejor que el baseline, no "
        "aporta valor."
    ),
    "Stacking (capa meta)": (
        "Técnica que combina dos modelos: uno base (XGBoost o LightGBM) "
        "que predice primero, y uno meta (un modelo aditivo simple) que "
        "ajusta esa predicción usando un puñado de variables clave. La idea "
        "es que el meta corrija sesgos del base sin re-aprender desde cero."
    ),
    "GAM (Modelo Aditivo)": (
        "Modelo simple e interpretable usado como capa meta. Aprende una "
        "curva suave por cada variable y suma las contribuciones — a "
        "diferencia de los árboles, no descubre interacciones nuevas, sólo "
        "afina la predicción del modelo base."
    ),
    "Auto-fallback": (
        "Mecanismo de seguridad del stacking: tras entrenar, el sistema "
        "compara el error del modelo base solo vs el del base+meta. Si la "
        "capa meta no mejora al base por al menos un margen mínimo, se "
        "desactiva automáticamente y la predicción de producción es la del "
        "base puro. Activar stacking nunca empeora el resultado."
    ),
}


def glossary_terms() -> List[Tuple[str, str]]:
    """Devuelve [(term, definition), ...] en orden de presentacion."""
    return list(GLOSSARY.items())


# -----------------------------------------------------------------------------
# Kit ejecutivo unificado
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class WinnerKit:
    """Bundle de datos derivados del campeon, listos para HTML / Excel.

    Centraliza el calculo de las entradas ejecutivas para que renderizadores
    (winner_dashboard.py, business_export.py) NUNCA reimplementen la
    construccion del kit. Si manana cambia la metrica que define
    'precision', se cambia aqui y los dos canales se actualizan a la vez.

    Attributes
    ----------
    real, pred, abs_err : arrays OOF en KG/JR alineados.
    X_aligned           : X_raw recortado por oof_mask (para subgrupos).
    oof_mape, oof_r2    : metricas OOF en KG/JR.
    abs_gap             : |MAE_test - MAE_train| del nested CV.
    verdict             : Verdict ejecutivo (icon + headline + body).
    context             : TrainingContext (filas, fechas, fundos, formatos).
    actions             : Lista de Action auto-generadas.
    stacking            : StackingDiagnostics si el campeón usa capa meta;
                          None en caso contrario. Se propaga al hero (pill)
                          y a la sección técnica (panel "Capa Meta").
    """

    real: np.ndarray
    pred: np.ndarray
    abs_err: np.ndarray
    X_aligned: Optional[pd.DataFrame]
    oof_mape: float
    oof_r2: Optional[float]
    abs_gap: float
    verdict: Verdict
    context: TrainingContext
    actions: List[Action]
    stacking: Optional[StackingDiagnostics] = None
    feature_importance: Optional["FeatureImportanceResult"] = None


def _abs_errors_aligned(
    champion: "ModelResult",
    X_raw: Optional[pd.DataFrame],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, Optional[pd.DataFrame]]:
    """Devuelve (real, pred, abs_err, X_aligned) en KG/JR OOF.

    Usa `business_validation` del campeon. X_aligned es X_raw recortado
    por `oof_mask` para que coincida en filas con real/pred.
    """
    bv = champion.business_validation
    real = np.asarray(getattr(bv, "kg_jr_real_oof", []) if bv else [], dtype=float)
    pred = np.asarray(getattr(bv, "kg_jr_pred_oof", []) if bv else [], dtype=float)
    abs_err = np.abs(pred - real) if real.size > 0 else np.array([])

    X_aligned: Optional[pd.DataFrame] = None
    mask = getattr(bv, "oof_mask", None) if bv else None
    if X_raw is not None and mask is not None:
        try:
            X_aligned = X_raw.iloc[np.asarray(mask, dtype=bool)].reset_index(drop=True)
        except Exception:
            X_aligned = None
    elif X_raw is not None and len(X_raw) == abs_err.size:
        X_aligned = X_raw.reset_index(drop=True)
    return real, pred, abs_err, X_aligned


def build_winner_kit(
    *,
    variety: str,
    champion: "ModelResult",
    X_raw: Optional[pd.DataFrame] = None,
    feature_importance: Optional["FeatureImportanceResult"] = None,
) -> WinnerKit:
    """Construye el kit ejecutivo del campeon en una sola pasada.

    Reglas:
      - Las metricas presentables al gerente son SIEMPRE OOF (honestas).
        In-sample (refit + predict all) es solo sanity check.
      - X_aligned se restringe via `oof_mask` cuando esta disponible para
        garantizar que los subgrupos del boxplot coincidan fila a fila
        con (real, pred).
    """
    real, pred, abs_err, X_aligned = _abs_errors_aligned(champion, X_raw)
    business_oof = champion.business_metrics_oof or {}
    oof_mape = float(business_oof.get("mape", float("nan")))
    oof_r2 = business_oof.get("r2")

    abs_gap = champion.abs_gap
    verdict = compute_verdict(full_mape_pct=oof_mape, abs_gap=abs_gap)
    context = build_context(variety, X_raw)
    actions = recommended_actions(
        abs_errors=abs_err, real=real, X_aligned=X_aligned,
        global_mape=oof_mape, abs_gap=abs_gap, full_mape=oof_mape,
    )
    stacking = getattr(champion, "stacking_diagnostics", None)
    return WinnerKit(
        real=real, pred=pred, abs_err=abs_err, X_aligned=X_aligned,
        oof_mape=oof_mape, oof_r2=oof_r2, abs_gap=abs_gap,
        verdict=verdict, context=context, actions=actions,
        stacking=stacking,
        feature_importance=feature_importance,
    )

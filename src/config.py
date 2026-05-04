"""Configuracion global del proyecto (LOCAL-ONLY).

Centraliza rutas, esquema de datos, hiperparametros de CV y URI de MLflow.
Cualquier modulo debe leer constantes desde aqui en vez de hardcodearlas.

Variables de entorno reconocidas (todas opcionales, con fallback sano):
    MLFLOW_TRACKING_URI       : URI del tracking server. Default: file://
                                hacia `mlruns/` local. Setear solo si se
                                apunta a un MLflow externo.
    MLFLOW_EXPERIMENT_PREFIX  : prefijo de experimentos MLflow.
                                Default vacio -> el experimento es la variedad.
    MODEL_REGISTRY_PREFIX     : prefijo del Model Registry.
                                Default 'rnd-forest-'.
    REPORT_PLOTLY_OFFLINE     : 1 = embeber plotly.js (default), 0 = CDN.

Esquema de modelado (decidido tras EDA):
    Target          : KG/JR_H (kg cosechados por jornal-hora)
    Numericas (raw) : KG/HA, %INDUS, DPC, P/BAYA, HA, DIA_COSECHA
    Categoricas     : FORMATO, FUNDO
    Date-derived    : ANIO, MES_SIN/COS (orden 1-3), SEMANA_SIN/COS,
                      DIA_SEM_SIN/COS, TEMPORADA_ALTA/BAJA  (creadas en FeatureGenerator)
    Structural      : KG_TOTAL, INDUS_KG_HA, KG_PER_BAYA, KG_HA_PER_DPC
                      (ratios intra-fila en FeatureGenerator)
    Lag features    : ~31 cols rolling/seasonal/ratios por (FUNDO+FORMATO,
                      FUNDO, FORMATO) en step_03_features/lag_features.py.

Excluidas por LEAKAGE (target = KG/JR / H-EF, demostrado con max_abs_diff = 0):
    KG/JR, H-EF

Excluidas por NULA INFORMACION (1 unico valor o MI = 0 en EDA):
    VARIEDAD, CALIBRADO, DIA_SEM
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Rutas del proyecto (resueltas desde la raiz)
# ---------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).resolve().parent.parent

DATA_DIR: Path = BASE_DIR / "data"
LOGS_DIR: Path = BASE_DIR / "logs"
ARTIFACTS_DIR: Path = BASE_DIR / "artifacts"
REPORTS_DIR: Path = BASE_DIR / "reports"
MLRUNS_DIR: Path = BASE_DIR / "mlruns"

# ---------------------------------------------------------------------------
# S3 — artifacts remotos (activo solo si S3_ARTIFACTS_BUCKET esta definido)
# ---------------------------------------------------------------------------
# En local queda vacio -> sin upload. En EC2/CI se inyecta via env var:
#   S3_ARTIFACTS_BUCKET=mi-bucket
#   S3_ARTIFACTS_PREFIX=ml-training/artifacts   (opcional, default '')
# El upload ocurre al final de main.py si el bucket esta configurado.
S3_ARTIFACTS_BUCKET: str = os.environ.get("S3_ARTIFACTS_BUCKET", "")
S3_ARTIFACTS_PREFIX: str = os.environ.get("S3_ARTIFACTS_PREFIX", "ml-training")
S3_REPORTS_PREFIX: str = os.environ.get("S3_REPORTS_PREFIX", "ml-training/reports")

# `mlruns/` solo se crea cuando el backend MLflow es file:// (caso default
# del proyecto local-only). Si MLFLOW_TRACKING_URI apunta a un server
# externo el dir queda inerte (no se crea para no producir ruido).
_tracking_uri_raw = os.environ.get("MLFLOW_TRACKING_URI", "")
_use_local_mlruns = (not _tracking_uri_raw) or _tracking_uri_raw.startswith("file:")


def init_dirs() -> None:
    """Crea los directorios de salida en disco. Idempotente.

    Se invoca explicitamente desde `main.py` / workers para evitar
    side-effects al importar `src.config` (un test que solo importe TARGET
    no debe crear `logs/`, `artifacts/`, etc.).
    """
    dirs: list[Path] = [LOGS_DIR, ARTIFACTS_DIR, REPORTS_DIR]
    if _use_local_mlruns:
        dirs.append(MLRUNS_DIR)
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Datos de entrada
# ---------------------------------------------------------------------------
ACCUMULATED_FILE: Path = DATA_DIR / "BD_HISTORICO_ACUMULADO.xlsx"
TRAINING_FILE: Path = DATA_DIR / "training" / "DB-HISTORICA.xlsx"
DEFAULT_VARIETIES: str = "POP"  # comma-separated; "all" expande a todas las hojas
MIN_ROWS_PER_VARIETY: int = 100  # umbral usado por scripts/prepare_data.py

# ---------------------------------------------------------------------------
# Esquema de modelado
# ---------------------------------------------------------------------------
TARGET: str = "KG/JR_H"

# Columnas numericas conservadas tal cual del Excel
NUMERIC_FEATURES: list[str] = ["KG/HA", "%INDUS", "DPC", "P/BAYA", "HA", "DIA_COSECHA"]

# Categoricas a one-hot
CATEGORICAL_FEATURES: list[str] = ["FORMATO", "FUNDO"]

# Columna de fecha (se transforma a derivadas ciclicas en FeatureGenerator)
DATE_COLUMN: str = "FECHA"

# Columnas que el data_loader debe traer del Excel (sin TARGET)
RAW_FEATURE_COLUMNS: list[str] = NUMERIC_FEATURES + CATEGORICAL_FEATURES + [DATE_COLUMN]

# Columnas a descartar explicitamente (leakage o nula informacion) si existieran
LEAKAGE_COLUMNS: list[str] = ["KG/JR", "H-EF"]
USELESS_COLUMNS: list[str] = ["VARIEDAD", "DIA_SEM", "MES"]

# ---------------------------------------------------------------------------
# Hiperparametros de CV y tuning
# ---------------------------------------------------------------------------
RANDOM_STATE: int = 42
# 'auto' = entrena TODOS los backends disponibles (xgb + lgb hoy) cada uno
# con su Optuna study independiente, y `champion.select_champion` elige el
# mejor por variedad usando composite_score (MAE_test penalizado por overfit).
# Pasa "xgb" o "lgb" explicito si quieres saltarte la comparacion (ahorra
# ~50% del tiempo). Pasa "xgb,lgb" o "all" para el mismo efecto que "auto".
MODEL_TYPE_DEFAULT: str = "auto"

# ---------------------------------------------------------------------------
# Quality gates del campeon (umbrales minimos para considerar un modelo util)
# ---------------------------------------------------------------------------
# Un campeon que no supere estos umbrales se considera inutilizable y NO
# se registra ni promueve en MLflow Registry. Los logs y artefactos se
# guardan igual para auditoria.
#
# CHAMPION_MAX_MAPE: MAPE maximo aceptable en data completa (full refit).
#   Valor en % (ej: 30.0 = 30%). Si el campeon supera este umbral,
#   el modelo no se promueve. Ajustar segun baseline del negocio.
# CHAMPION_MAX_GAP: brecha maxima Train-Test aceptable (overfitting).
#   Valor en % (ej: 15.0 = 15pp de diferencia entre MAPE_train y MAPE_test).
CHAMPION_MAX_MAPE: float = float(os.environ.get("CHAMPION_MAX_MAPE", "30.0"))
CHAMPION_MAX_GAP: float = float(os.environ.get("CHAMPION_MAX_GAP", "15.0"))

# ---------------------------------------------------------------------------
# Decision lex-order del champion (champion.select_champion)
# ---------------------------------------------------------------------------
# Estos umbrales gobiernan el desempate entre modelos (XGB vs LGB) en
# `select_champion`. Centralizados aqui para tunear sin tocar codigo.

# Tolerancia "practica" sobre el |gap|. Dos modelos cuyo |gap| difiere en
# menos de esto se consideran empate en estabilidad. 0.5 pp del KG/JR_H
# suele estar dentro del ruido de CV para datasets de 1k-10k filas.
GAP_TIE_TOLERANCE: float = 0.005

# Tolerancia sobre MAPE total (en %). Empate de rendimiento -> desempata por
# tiempo. 0.5 pp de MAPE es ruido tipico entre seeds distintas.
FULL_MAPE_TIE_TOLERANCE: float = 0.5

# Si el meta GAM mejora al base por mas de este porcentaje (delta_pct < -X),
# se considera que aporta valor real y se desempata a favor del modelo con
# stacking activo. Por debajo de este umbral la mejora cae en ruido de CV.
META_PREFERENCE_DELTA_PCT: float = -2.0

# Auto-fallback del StackedRegressor: el meta debe mejorar el MAE del base
# en al menos esta proporcion para no caer al base puro. 0.005 = 0.5%.
STACKING_FALLBACK_RELATIVE_MARGIN: float = 0.005

# ---------------------------------------------------------------------------
# Tuning profiles (presupuesto de Optuna: cuantos trials, cuantos folds).
# NO confundir con entornos (local vs aws): el tuning es ortogonal al entorno.
#
# Tiempo estimado para 10k filas, 16 features, modelos XGB/LGB:
#   smoke   : ~1 min   (verificar que nada se rompe)
#   dev     : ~10 min  (iteracion durante desarrollo)
#   prod    : ~1.5 h   (modelo a promover)
#   prod_xl : ~2.5 h   (baseline contra el cual medir si mas trials mueven MAPE)
#
# `meta_trials` se aplica SOLO si --stacking gam. Se ejecuta DESPUES del
# nested CV del base, sobre las OOF preds que produce StackedRegressor.fit
# (ver Opcion C en la nota de diseno de stacking.py). 0 = sin tuning del
# meta (usa STACKING_GAM_N_SPLINES / STACKING_GAM_LAM como defaults).
# Tiempo extra: ~meta_trials * 30s para dataset 10k filas.
TUNING_PROFILES: dict[str, dict[str, int]] = {
    "smoke": {
        "n_trials": 5,
        "final_trials": 3,
        "outer_folds": 2,
        "inner_folds": 2,
        "meta_trials": 0,
    },
    "dev": {
        "n_trials": 20,
        "final_trials": 10,
        "outer_folds": 3,
        "inner_folds": 3,
        "meta_trials": 20,
    },
    "prod": {
        "n_trials": 60,
        "final_trials": 30,
        "outer_folds": 5,
        "inner_folds": 3,
        "meta_trials": 30,
    },
    "prod_xl": {
        "n_trials": 100,
        "final_trials": 50,
        "outer_folds": 5,
        "inner_folds": 3,
        "meta_trials": 50,
    },
}
DEFAULT_TUNING: str = "dev"

# Defaults de CV (usados por tuning.py cuando el caller no override-a)
OUTER_CV_FOLDS: int = TUNING_PROFILES[DEFAULT_TUNING]["outer_folds"]
INNER_CV_FOLDS: int = TUNING_PROFILES[DEFAULT_TUNING]["inner_folds"]

# Refit final: K pipelines en folds del KFold; predict promedia las K.
# Reduce varianza del modelo de produccion (~5-10%) a costa de +(K-1)x
# tiempo del refit final, que es despreciable vs el nested CV.
# K=1 = legacy (refit unico sobre todo el dataset).
OOF_ENSEMBLE_K: int = 5

# ---------------------------------------------------------------------------
# Stacking (capa meta sobre el campeon XGB/LGB)
# ---------------------------------------------------------------------------
# "none" = campeon directo (default actual). "gam" = envuelve el campeon
# en `StackedRegressor` con un GAM (pyGAM) como meta-learner.
# El meta recibe [pred_base, X_subset] y emite la prediccion final.
#
# Por que el default es "none" (mayo 2026): en POP el GAM con el x_subset
# actual (KG/HA, %INDUS, DIA_COSECHA, P/BAYA, FORMATO) cae a fallback en
# 100% de los casos -- esas 5 features YA estan en el base XGB/LGB y el
# GAM aditivo es estrictamente menos expresivo que el base con
# interacciones. Resultado: -7-9% MAE vs base, fallback automatico, +5
# minutos de computo por nada. Para que el meta aporte, el x_subset debe
# incluir features que el base NO ve (estadisticos agregados por FUNDO,
# residuos, etc.); hasta tener ese diseño, default = "none".
STACKING_DEFAULT: str = "none"

# Columnas raw que el GAM recibe ADEMAS de pred_base. Mantener corto
# (3-7 features) evita curse-of-dim del GAM. Mezcla continuas + categoricas:
# StackedRegressor detecta el dtype y elige `s()` (spline) o `f()` (factor)
# automaticamente. Categoricas: label-encoded con map memorizado en fit.
# Continuas con NaN ratio > STACKING_NAN_FLAG_THRESHOLD: se auto-genera
# una flag <col>__ISNAN como factor adicional (evita que el spline se
# pegue al pico de la mediana imputada).
#
# Decisiones del subset actual:
#   - KG/HA, %INDUS, DIA_COSECHA  : continuas con MI alta y curvas suaves.
#   - P/BAYA                       : continua con 39% missing -> spline +
#                                    flag __ISNAN auto-generada (factor).
#   - FORMATO                      : categorica -> factor por nivel (f()).
#                                    Estabiliza overfit en formatos chicos
#                                    donde un spline se pegaria al ruido.
STACKING_X_SUBSET: list[str] = [
    "KG/HA",
    "%INDUS",
    "DIA_COSECHA",
    "P/BAYA",
    "FORMATO",
]

# Folds del cross_val_predict interno para construir las OOF preds que
# alimentan al GAM en fit. K=5 es estandar; bajar a 3 para `--tuning dev`.
STACKING_OOF_FOLDS: int = 5

# Defaults del GAM (pueden ser tuneados por Optuna via search_spaces).
# `lam=1.5` (subido desde 0.6) prioriza smoothness sobre fit: data con
# cola larga + gap CV->prod 3-5pp se beneficia mas de splines suaves
# que de wiggle. Tunear con Optuna si una variedad pide otra cosa.
STACKING_GAM_N_SPLINES: int = 15
STACKING_GAM_LAM: float = 1.5

# NaN ratio a partir del cual se auto-genera la flag <col>__ISNAN para
# una columna continua del subset. El factor binario captura la senal
# "imputed vs observed" sin que el spline alise el paso 0->1 como continuo.
STACKING_NAN_FLAG_THRESHOLD: float = 0.10

# Auto-fallback: tras fit, comparar MAE del base puro vs MAE del meta
# sobre las OOF preds. Si el meta no mejora al base por mas de un
# margen pequeno (~0.5%), `predict()` cae al base puro. Garantiza
# que activar stacking nunca empeora vs el campeon.
STACKING_AUTO_FALLBACK: bool = True

# Tuning del meta GAM con Optuna. Se ejecuta sobre las OOF preds que
# StackedRegressor.fit ya genera (Opcion C: reusar oof_pred del propio
# stacked, no del nested CV del base). 0 = sin tuning (usa defaults).
# El presupuesto recomendado vive en TUNING_PROFILES[<perfil>]["meta_trials"];
# este valor es el fallback cuando el caller no override-a.
STACKING_META_TRIALS_DEFAULT: int = 0

# KFold interno del Optuna del meta. Un fold separado del OOF del base
# evita que el GAM se entrene y valide sobre las MISMAS predicciones
# (overfit del meta). 3 es un balance velocidad/varianza razonable.
STACKING_META_INNER_FOLDS: int = 3

# ---------------------------------------------------------------------------
# MLflow
# ---------------------------------------------------------------------------
# Default: backend local file:// hacia ./mlruns. Si se setea
# MLFLOW_TRACKING_URI en el entorno, se respeta (permite apuntar a un
# MLflow server externo si el caso lo amerita). Para uso local-only
# normal NO requiere ningun .env.
MLFLOW_TRACKING_URI: str = _tracking_uri_raw or MLRUNS_DIR.as_uri()

# Prefijo del nombre de experimento. El experimento final por variedad sera
# `f"{MLFLOW_EXPERIMENT_PREFIX}{variety}"`.
# Default vacio: el nombre del experimento es la VARIEDAD (e.g. "POP").
# Esto crea UN experimento INDEPENDIENTE por variedad, y cada training
# es un run versionado (e.g. "xgb_v3") dentro de ese experimento.
MLFLOW_EXPERIMENT_PREFIX: str = os.environ.get("MLFLOW_EXPERIMENT_PREFIX", "")

# Prefijo del Model Registry (registered model = `f"{prefix}{variety}"`).
# Cada training de la misma variedad genera una nueva VERSION del mismo
# registered model. Con backend file:// el Registry NO esta disponible
# (mlflow_registry.register_model devuelve None silenciosamente). Para
# registrar versionado real se requiere un MLflow server con backend SQL
# (ej. SQLite, Postgres) accesible via MLFLOW_TRACKING_URI.
MODEL_REGISTRY_PREFIX: str = os.environ.get("MODEL_REGISTRY_PREFIX", "rnd-forest-")

# ---------------------------------------------------------------------------
# Branding del reporte gerencial
# ---------------------------------------------------------------------------
REPORT_PROJECT_NAME: str = "Pronostico de productividad de cosecha (POP)"
REPORT_BUSINESS_UNIT: str = "Operaciones Agricolas"
REPORT_TARGET_LABEL: str = "kg por jornal-hora"

# Umbrales semaforo del reporte (sobre R2 mean del nested CV)
REPORT_R2_GOOD: float = 0.85  # >= verde / "Excelente"
REPORT_R2_WARN: float = 0.70  # >= amarillo / "Aceptable", < rojo / "Insuficiente"

# Targets gerenciales que se renderizan como gauges en el HTML.
# Mover la aguja por encima/debajo de estos valores cambia el color del gauge.
REPORT_R2_TARGET: float = 0.90  # R2 gerencial (KG/JR OOF) que el negocio quiere superar
REPORT_MAE_TARGET: float = (
    0.20  # MAE del modelo (KG/JR_H Test CV) que el negocio quiere NO superar
)

# Descripcion en lenguaje natural del modelo. Aparece en el hero del
# dashboard ejecutivo para que un lector no-tecnico entienda en 1 frase
# que predice el modelo, en que unidad y para que sirve.
REPORT_MODEL_DESCRIPTION: str = (
    "Predice la productividad por jornal (kilogramos cosechados por "
    "jornada de trabajo) usando datos historicos de cosecha, formato del "
    "producto, fundo y fechas. Permite anticipar el rendimiento esperado "
    "para planificar logistica, equipos y compromisos comerciales."
)

# Veredicto ejecutivo: combina MAPE de negocio (sobre data total) y
# brecha train-test (overfitting) para clasificar el modelo en 4 niveles.
# Cada nivel mapea a un semaforo + recomendacion accionable.
#
# Lectura: el modelo cae en el nivel mas conservador donde AMBAS metricas
# entren. Ej.: MAPE=12% (nivel 1 OK) pero gap=0.30 (nivel 4 OUT) -> nivel 3.
REPORT_VERDICT_THRESHOLDS: dict = {
    "alta_confianza": {"max_mape_pct": 15.0, "max_abs_gap": 0.10},
    "confianza_aceptable": {"max_mape_pct": 22.0, "max_abs_gap": 0.18},
    "confianza_limitada": {"max_mape_pct": 35.0, "max_abs_gap": 0.30},
    # peor que confianza_limitada -> "no_recomendado"
}

# Subgroup MAPE multiplier sobre el global a partir del cual un FORMATO/FUNDO
# se marca como "problematico" en la seccion de Acciones Recomendadas.
REPORT_SUBGROUP_WARN_RATIO: float = 1.5  # >= 1.5x el MAPE global = warning

# Tamano minimo de un subgrupo (FORMATO o FUNDO) para que cuente como
# candidato a "problematico". Mas chico = ruido puro.
REPORT_SUBGROUP_MIN_N: int = 10

# Umbrales de tarjetas KPI ejecutivas (lenguaje natural). Cambiar aqui mueve
# tanto el HTML como el Excel sin tocar codigo.
KPI_PRECISION_HIGH_MAPE_PCT: float = 15.0  # MAPE <= 15 -> ALTO
KPI_PRECISION_MEDIUM_MAPE_PCT: float = 25.0  # MAPE <= 25 -> MEDIO, sino BAJO
KPI_R2_HIGH_PCT: float = 80.0  # R2*100 >= 80 -> ALTO
KPI_R2_MEDIUM_PCT: float = 60.0  # >= 60 -> MEDIO, sino BAJO
KPI_BASELINE_HIGH_IMPROVEMENT_PCT: float = 50.0
KPI_BASELINE_MEDIUM_IMPROVEMENT_PCT: float = 25.0

# Umbrales para acciones recomendadas auto-generadas.
ABS_GAP_WARN: float = 0.20  # |gap| > 0.20 = "memorizo entrenamiento"
FULL_MAPE_CRITICAL_PCT: float = 25.0  # MAPE > 25% = critico

# Group-rare en data_loader: categorias con n<RARE_MIN_COUNT se colapsan en
# 'OTROS'. Solo se aplica a las columnas listadas en RARE_GROUP_COLS.
RARE_MIN_COUNT: int = 50
RARE_GROUP_COLS: list[str] = ["FORMATO"]

# Estado semaforo (VERDE/AMARILLO/ROJO) en la hoja Resumen del Excel:
#   R2 OOF >= REPORT_R2_TARGET            -> VERDE
#   R2 OOF >= REPORT_R2_AMBER_THRESHOLD   -> AMARILLO, sino ROJO
#   MAE   <= REPORT_MAE_TARGET            -> VERDE
#   MAE   <= REPORT_MAE_TARGET * REPORT_MAE_AMBER_RATIO -> AMARILLO, sino ROJO
REPORT_R2_AMBER_THRESHOLD: float = 0.70
REPORT_MAE_AMBER_RATIO: float = 2.0

# Modo de carga de plotly.js en el HTML:
#   True  = embebido inline (+~4.5 MB al HTML, autocontenido, funciona sin internet)
#   False = CDN script (HTML ~1MB pero requiere internet para renderizar charts)
# Override desde env: REPORT_PLOTLY_OFFLINE=0 (CDN) o =1 (embebido).
REPORT_PLOTLY_OFFLINE: bool = os.environ.get("REPORT_PLOTLY_OFFLINE", "1") != "0"

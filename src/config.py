"""Configuracion global del proyecto.

Centraliza rutas, esquema de datos, hiperparametros de CV y URI de MLflow.
Cualquier modulo debe leer constantes desde aqui en vez de hardcodearlas.

Backend MLflow:
    El proyecto SIEMPRE usa un MLflow server (Postgres + S3 detras).
    En local lo sirve `docker compose up` (servicio mlflow en :5000,
    backend Postgres + S3 real parametrizado via S3_MLFLOW_BUCKET).
    En produccion apuntas la misma env var `MLFLOW_TRACKING_URI` a tu
    server real (ECS Fargate detras de ALB). No hay backend file://mlruns
    ni sqlite local ni LocalStack (ADR-001 / ADR-003).

Variables de entorno reconocidas (todas opcionales, con fallback sano):
    MLFLOW_TRACKING_URI       : URI del tracking server. Default:
                                http://localhost:5000 (servicio Docker
                                expuesto al host). En el container del
                                trainer se sobreescribe a http://mlflow:5000
                                via docker-compose.yml.
    MLFLOW_EXPERIMENT_PREFIX  : prefijo de experimentos MLflow.
                                Default vacio -> el experimento es la variedad.
    MODEL_REGISTRY_PREFIX     : prefijo del Model Registry.
                                Default 'rnd-forest-'.
    REPORT_PLOTLY_OFFLINE     : 0 = CDN (default, recomendado), 1 = embeber plotly.js (offline).

Esquema de modelado (decidido tras EDA):
    Target          : KG/JR_H (kg cosechados por jornal-hora)
    Numericas (raw) : KG/HA, %INDUS, DPC, P/BAYA, HA, DIA_COSECHA
    Categoricas     : FORMATO, FUNDO
    Date-derived    : ANIO, MES_SIN/COS (orden 1-3), SEMANA_SIN/COS,
                      TEMPORADA_ALTA/BAJA  (creadas en FeatureGenerator)
                      DIA_SEM_SIN/COS removidas (auditoria 2026-05-05: corr ~0).
    Structural      : KG_TOTAL, INDUS_KG_HA, KG_PER_BAYA, KG_HA_PER_DPC
                      (ratios intra-fila en FeatureGenerator)
    Lag features    : 35 cols rolling/seasonal/std/slope/ratios + tenure + cadencia
                      por (FUNDO+FORMATO, FUNDO, FORMATO) en step_03_features/
                      lag_features.py (ver LAG_OUTPUT_COLUMNS).

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

# ---------------------------------------------------------------------------
# S3 — artifacts remotos (activo solo si S3_ARTIFACTS_BUCKET esta definido)
# ---------------------------------------------------------------------------
# Apunta SIEMPRE a un bucket S3 real (ADR-003: no usamos LocalStack).
# En local lo configurás vía .env (S3_ARTIFACTS_BUCKET=<tu-bucket>).
# En AWS Batch lo inyecta la job-def definida en GUIA_MLOPS_AWS.md §4.4.
# El upload ocurre al final de main.py si el bucket esta configurado;
# scripts/s3_sync.py es defensivo: si S3 falla, el training termina OK
# igual y los artefactos quedan en disco local del container.
S3_ARTIFACTS_BUCKET: str = os.environ.get("S3_ARTIFACTS_BUCKET", "")
S3_ARTIFACTS_PREFIX: str = os.environ.get("S3_ARTIFACTS_PREFIX", "ml-training")
S3_REPORTS_PREFIX: str = os.environ.get("S3_REPORTS_PREFIX", "ml-training/reports")


def init_dirs() -> None:
    """Crea los directorios de salida en disco. Idempotente.

    Se invoca explicitamente desde `main.py` / workers para evitar
    side-effects al importar `src.config` (un test que solo importe TARGET
    no debe crear `logs/`, `artifacts/`, etc.).
    """
    for d in (LOGS_DIR, ARTIFACTS_DIR, REPORTS_DIR):
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

# Missing flags: columnas con missing significativo cuyo NaN es informativo.
# `MissingFlagger` agrega `<col>__MISS` antes del imputer para que el modelo
# reciba la senal de "esta fila tenia ese valor faltante". Decision basada
# en EDA POP (filas con P/BAYA NaN -> MAPE 17.3% vs 15.6% observadas;
# %INDUS similar). Si entrenas otra variedad con patrones distintos,
# pasar `cols=` explicito al constructor de `MissingFlagger` o ajustar aqui.
MISSING_FLAG_COLS: list[str] = ["%INDUS", "P/BAYA"]

# Skew mitigation: thresholds para auto-deteccion en FeatureGenerator.fit.
# Reemplazo las listas hardcoded por variedad (eran fragiles cuando se entrena
# variedades nuevas con distribuciones distintas). FeatureGenerator decide
# por columna si log1p / sqrt aplica, basado en skew y kurtosis del fit data.
#
# Politica de transformacion (additive: no reemplaza la columna raw):
#   - kurt > SKEW_KURT_THRESHOLD       -> agrega <col>_SQRT
#   - |skew| > SKEW_THRESHOLD          -> agrega <col>_LOG1P
#   - else                              -> nada (distribucion sana)
#
# El shift por columna se memoiza en fit -> transform usa el mismo shift.
# Sin esto, la misma fila podia dar valores distintos en train vs inference
# cuando los rangos diferian (bug latente de la version anterior).
#
# SKEW_AUTO_DETECT permite desactivar para comparar contra baseline sin
# transformaciones (A/B test informativo).
SKEW_AUTO_DETECT: bool = True
SKEW_THRESHOLD: float = 1.5      # |skew| above this -> log1p
SKEW_KURT_THRESHOLD: float = 50.0  # kurtosis above this -> sqrt (mas agresivo)

# ---------------------------------------------------------------------------
# EDA thresholds (diagnostics/*.py)
# ---------------------------------------------------------------------------
# Reportar findings de skew/kurt en EDA. Independientes de SKEW_THRESHOLD
# y SKEW_KURT_THRESHOLD (que rigen transformacion en FeatureGenerator):
# aqui solo se trata de avisar al humano en el reporte de auditoria.
EDA_KURT_WARN: float = 5.0          # kurt > 5 -> finding "medium"
EDA_KURT_HIGH: float = 10.0         # kurt > 10 -> escala el finding a "high"
EDA_SKEW_HIGH: float = 3.0          # |skew| > 3 -> escala el finding a "high"

# Fraccion de outliers IQR sobre n_total que dispara warning en EDA.
OUTLIER_FRACTION_WARN: float = 0.05

# Threshold para considerar dos numericas "muy correlacionadas". Se usa en
# diagnostics/multivariate.py:correlation_matrix y en eda.py al llamarla.
CORRELATION_HIGH_THRESHOLD: float = 0.85

# Cardinalidad de variables categoricas (diagnostics/categorical.py).
#   CARDINALITY_HIGH : por encima de esto, NO se calcula chi2/Cramer's V
#                      (tabla de contingencia se vuelve poco confiable).
#   CARDINALITY_WARN : aviso para considerar target-encoding / agrupar.
CARDINALITY_HIGH: int = 200
CARDINALITY_WARN: int = 50

# Bandas de interpretacion de V de Cramer (asociacion categorica-target).
#   < CRAMERS_V_WEAK   : asociacion debil, candidata a drop / agrupar.
#   >= CRAMERS_V_STRONG: asociacion fuerte, target-encoding util.
CRAMERS_V_WEAK: float = 0.05
CRAMERS_V_STRONG: float = 0.3

# ---------------------------------------------------------------------------
# Hiperparametros de CV y tuning
# ---------------------------------------------------------------------------
RANDOM_STATE: int = 42
# El pipeline siempre entrena TODOS los backends del registry (XGB + LGB
# hoy) cada uno con su Optuna study independiente, y `champion.select_champion`
# elige el mejor por variedad usando lex-order (gap -> full_mape -> tiempo).
# Si en el futuro se agrega un nuevo backend al BACKEND_REGISTRY, queda
# incluido automaticamente.

# ---------------------------------------------------------------------------
# Quality gates del campeon (umbrales minimos para considerar un modelo util)
# ---------------------------------------------------------------------------
# Un campeon que no supere estos umbrales se considera inutilizable y NO
# se registra ni promueve en MLflow Registry. Los logs y artefactos se
# guardan igual para auditoria.
#
# CHAMPION_MAX_MAPE: MAPE OOF maximo aceptable (out-of-fold, honesto).
#   Valor en % (ej: 25.0 = 25%). Si el campeon supera este umbral,
#   el modelo no se promueve. Comparado contra `champion.oof_mape`
#   (cada fila predicha por un modelo que NO la vio en train).
#   25% es ~8pp arriba del MAPE_oof observado en POP (~17%); deja
#   holgura para variedades mas dificiles pero filtra modelos rotos.
#   Antes era 30% comparado contra full_mape (in-sample, optimista).
# CHAMPION_MAX_GAP: brecha maxima Train-Test aceptable (overfitting).
#   Valor en % (18.0 = 18pp de diferencia entre MAE_train y MAE_test).
#   Subido de 15 -> 18 tras evidencia empirica: con search spaces rev. 7.1
#   (LGB) y rev. 6 (XGB) -- capacidad capada y regularizacion estricta
#   forzada -- el suelo realista del gap para POP (10k filas, 16 estratos
#   FUNDO_FORMATO, target con cola larga) ronda 0.13-0.18pp. 15pp era
#   conservador sin restricciones de capacidad, y rechazaba modelos que
#   ya combatieron el overfit pero rebotan contra el techo del dataset.
#   Un overfit "real" (Optuna gaming el search space) deja gaps de 20+pp,
#   que este threshold sigue rechazando correctamente.
CHAMPION_MAX_MAPE: float = float(os.environ.get("CHAMPION_MAX_MAPE", "25.0"))
CHAMPION_MAX_GAP: float = float(os.environ.get("CHAMPION_MAX_GAP", "18.0"))

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


# ---------------------------------------------------------------------------
# Tuning profiles (presupuesto de Optuna: cuantos trials, cuantos folds).
# NO confundir con entornos (local vs aws): el tuning es ortogonal al entorno.
#
# Tiempo estimado para 10k filas, 16 features, modelos XGB/LGB:
#   smoke   : ~1 min      (verificar que nada se rompe)
#   dev     : ~20 min     (iteracion durante desarrollo)
#   prod    : ~1.5-2.5 h  (modelo a promover)
#   prod_xl : ~5-6 h      (baseline overnight: +1 outer fold y 2x trials vs prod)
TUNING_PROFILES: dict[str, dict[str, int]] = {
    "smoke": {
        "n_trials": 5,
        "final_trials": 3,
        "outer_folds": 2,
        "inner_folds": 2,
    },
    "dev": {
        "n_trials": 20,
        "final_trials": 10,
        "outer_folds": 3,
        "inner_folds": 3,
    },
    "prod": {
        "n_trials": 60,
        "final_trials": 30,
        "outer_folds": 5,
        "inner_folds": 3,
    },
    "prod_xl": {
        "n_trials": 100,
        "final_trials": 50,
        "outer_folds": 6,
        "inner_folds": 3,
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

# Sample weights por densidad del target (compute_sample_weights).
# Centralizado aqui para tunear sin tocar codigo. n_bins=10 fija el valor
# que el caller usaba de facto (antes hardcoded en tuning.py overrideando
# el default 20 de la funcion); cap=5.0 alinea con el default historico.
SAMPLE_WEIGHT_BINS: int = 10
SAMPLE_WEIGHT_CAP: float = 5.0

# ---------------------------------------------------------------------------
# Feature flags para ABLATION (env-var driven, default = legacy)
# ---------------------------------------------------------------------------
# Cada cambio del plan FE 2026-05-09 se activa selectivamente. Default
# todos OFF -> comportamiento equivalente al modelo LGB v3 (MAPE_oof
# 14.86%, gap 0.138) baseline. Activar uno a uno (smoke train ~1min) y
# comparar MAPE_oof + gap para ver cual aporta.
#
# Ejemplo:
#   docker compose run --rm \
#     -e ENABLE_OUTLIER_CASCADE_FF=1 \
#     -e CV_OUTER_STRATEGY=temporal_year \
#     trainer --varieties POP --tuning smoke


def _env_bool(name: str, default: bool = False) -> bool:
    """Lee bool de env var: '1', 'true', 'yes' (case-insensitive) -> True."""
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


# A — OutlierCapper: bounds por (FUNDO, FORMATO) con cascade fallback.
#     Justificacion: 86% del data es FORMATO=GRANEL y 72% FUNDO=A9; bounds
#     globales o solo-por-FUNDO los reflejan a ellos y cortaban grupos
#     chicos (CLAMSHELL 11 OZ con target μ=2.54 vs 5.35 GRANEL).
#     False (default) = group_col="FUNDO" legacy.
ENABLE_OUTLIER_CASCADE_FF: bool = _env_bool("ENABLE_OUTLIER_CASCADE_FF", False)

# D — Lags simples shift(1)/shift(2) + diff(1) por (FUNDO, FORMATO).
#     Justificacion: PACF de POP muestra lag 1=0.50, lag 2=0.33 (los mas
#     fuertes). Las rolling medians 7/14/30/90 ya existentes pueden
#     suavizar esa senal puntual.
#     Riesgo: alta correlacion con KG_JR_H_lag_FF_7 -> ruido.
ENABLE_SIMPLE_LAGS: bool = _env_bool("ENABLE_SIMPLE_LAGS", False)

# F — FUNDO_FORMATO interaction como dummies (15-18 cols).
#     Validado en prod_xl POP (2026-05-09) vs LGB v3 baseline:
#       - gap promedio mejora -0.011 (-8%): 0.138 -> 0.127.
#       - MAE_test marginal +0.004 (dentro del std=0.016, ruido).
#       - biz_MAPE_oof marginal +0.07pp (ruido vs baseline 14.86%).
#       - std gap +0.021 (mas inestable, fold 4 outlier con gap=0.225).
#     Cambio default OFF -> ON: el gap MEJORA real (8%) justifica el cambio
#     a pesar de la mayor varianza. La interaccion FUNDO_FORMATO es senal
#     legitima (V=0.26 vs target). Se mantiene como flag por si alguna
#     variedad futura no se beneficie (V<0.10 -> override con env=0).
ENABLE_FUNDO_FORMATO_INTERACTION: bool = _env_bool(
    "ENABLE_FUNDO_FORMATO_INTERACTION", True,
)

# G — CV outer strategy.
#   "stratified"     : StratifiedKFold por FUNDO_FORMATO (legacy, default).
#   "temporal_year"  : TemporalYearSplit expanding-window por ANIO.
#     Justificacion: drift severo (PSI hasta 2.09) entre anios consecutivos
#     hace que stratified mezcle train/test del futuro -> MAE_test
#     artificialmente bajo. Temporal mide error real bajo drift.
# Inner CV siempre stratified (Optuna trial scope: equilibrio por estrato).
CV_OUTER_STRATEGY: str = os.environ.get("CV_OUTER_STRATEGY", "stratified")
TEMPORAL_CV_MIN_TRAIN_YEARS: int = int(os.environ.get("TEMPORAL_CV_MIN_TRAIN_YEARS", "2"))

# ---------------------------------------------------------------------------
# MLflow
# ---------------------------------------------------------------------------
# El proyecto SIEMPRE corre contra un MLflow server (Postgres backend +
# S3 artifact-root). En local lo provee `docker compose up` (servicio
# `mlflow`). En produccion apuntas la misma env var a tu server real.
#
# Default = http://localhost:5000 = el servicio Docker expuesto al host
# (asi corren scripts que ejecutan en el host, ej. utilidades manuales).
# DENTRO del container del trainer se sobreescribe a http://mlflow:5000
# via docker-compose.yml.
#
# El `artifact_location` lo decide el server (--default-artifact-root
# s3://ml-mlflow/artifacts). El client NO debe pasarlo: si lo hiciera
# rompe el modelo (apuntaria a un path local del cliente que el server
# no puede leer).
MLFLOW_TRACKING_URI: str = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")

# Prefijo del nombre de experimento. El experimento final por variedad sera
# `f"{MLFLOW_EXPERIMENT_PREFIX}{variety}"`.
# Default vacio: el nombre del experimento es la VARIEDAD (e.g. "POP").
# Esto crea UN experimento INDEPENDIENTE por variedad, y cada training
# es un run versionado (e.g. "xgb_v3") dentro de ese experimento.
MLFLOW_EXPERIMENT_PREFIX: str = os.environ.get("MLFLOW_EXPERIMENT_PREFIX", "")

# Prefijo del Model Registry (registered model = `f"{prefix}{variety}"`).
# Cada training de la misma variedad genera una nueva VERSION del mismo
# registered model. ADR-001 garantiza que SIEMPRE corremos contra un MLflow
# server con backend SQL (Postgres en local + AWS), por lo que el Registry
# esta disponible incondicionalmente.
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
#   False = CDN (default) — HTML ~4MB Winner, ~1.5MB EDA. Requiere internet para renderizar charts.
#   True  = embebido inline (+~4.5MB al HTML, autocontenido, funciona offline).
# Override desde env: REPORT_PLOTLY_OFFLINE=1 (embebido) o =0 (CDN).
# Default cambiado a CDN (2026-05-09): los reports se sirven via nginx local
# o S3 web hosting; ambos casos tienen internet. Para casos offline (email,
# archivo en avion) setear `REPORT_PLOTLY_OFFLINE=1` en ese run especifico.
REPORT_PLOTLY_OFFLINE: bool = os.environ.get("REPORT_PLOTLY_OFFLINE", "0") != "0"

# ---------------------------------------------------------------------------
# === HISTORIAL ===
# ---------------------------------------------------------------------------
# Notas historicas / decisiones de diseño preservadas como contexto. NO
# afectan la ejecucion: ningun codigo lee de aqui.
#
# CatBoost (2026-05-05): evaluado y eliminado del BACKEND_REGISTRY. No
#     aportaba en POP frente a XGB/LGB; mismo patron que GAMM Phase 0.
#     Si se reincorpora a futuro hay que reagregarlo al registry y al
#     pipeline de tuning.

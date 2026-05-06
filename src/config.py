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

# `mlruns/` se crea cuando el backend MLflow es local (file:// o sqlite://
# apuntando a mlruns/mlflow.db). Si MLFLOW_TRACKING_URI apunta a un server
# externo el dir queda inerte (no se crea para no producir ruido).
_tracking_uri_raw = os.environ.get("MLFLOW_TRACKING_URI", "")
_use_local_mlruns = (
    (not _tracking_uri_raw)
    or _tracking_uri_raw.startswith("file:")
    or "mlruns/mlflow.db" in _tracking_uri_raw
    or "mlruns\\mlflow.db" in _tracking_uri_raw
)


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

# Missing flags: columnas con missing significativo cuyo NaN es informativo.
# `MissingFlagger` agrega `<col>__MISS` antes del imputer para que el modelo
# reciba la senal de "esta fila tenia ese valor faltante". Decision basada
# en EDA POP (filas con P/BAYA NaN -> MAPE 17.3% vs 15.6% observadas;
# %INDUS similar). Si entrenas otra variedad con patrones distintos,
# pasar `cols=` explicito al constructor de `MissingFlagger` o ajustar aqui.
MISSING_FLAG_COLS: list[str] = ["%INDUS", "P/BAYA"]

# ---------------------------------------------------------------------------
# Hiperparametros de CV y tuning
# ---------------------------------------------------------------------------
RANDOM_STATE: int = 42
# 'auto' = entrena TODOS los backends del registry (xgb + lgb hoy) cada uno
# con su Optuna study independiente, y `champion.select_champion` elige el
# mejor por variedad usando lex-order (gap -> full_mape -> tiempo). Si en
# el futuro se agrega un nuevo backend al BACKEND_REGISTRY, "auto" lo
# incluye automaticamente.
#
# Pasa "xgb" o "lgb" explicito para uno solo (ahorra ~50% tiempo). "xgb,lgb"
# o "all" tambien validos. Nota: CatBoost fue evaluado y eliminado del
# proyecto 2026-05-05 (no aportaba en POP; mismo patron que GAMM Phase 0).
MODEL_TYPE_DEFAULT: str = "auto"

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

# ---------------------------------------------------------------------------
# MLflow
# ---------------------------------------------------------------------------
# Default: backend local SQLITE (sqlite:///mlruns/mlflow.db) -> habilita
# Model Registry real con versionado y stage transitions. Antes era
# file://mlruns/ que NO soporta Registry (register_model devolvia None).
# Con el comparativo multi-backend (xgb + lgb hoy) tener registry funcional
# es critico: cada training crea una nueva version del modelo registrado y
# las versiones perdedoras quedan archivadas.
#
# Si MLFLOW_TRACKING_URI esta en el entorno se respeta (apuntar a server
# externo o forzar file:// para casos legacy).
#
# `MLRUNS_ARTIFACT_LOCATION` se usa como `artifact_location` al crear
# experimentos (ver `mlflow_registry.set_experiment`). Esto mantiene los
# artifacts (pipelines, jsons, html) en `./mlruns/artifacts/<exp_id>/`,
# en lugar del default de MLflow `./mlartifacts/`. Sin esto, sqlite
# backend dispersaria metadata en mlruns/mlflow.db pero artifacts en
# mlartifacts/, fragmentando el local store.
MLFLOW_DEFAULT_DB: Path = MLRUNS_DIR / "mlflow.db"
MLFLOW_TRACKING_URI: str = _tracking_uri_raw or f"sqlite:///{MLFLOW_DEFAULT_DB.as_posix()}"
MLRUNS_ARTIFACT_LOCATION: str = (MLRUNS_DIR / "artifacts").as_uri()

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

"""Configuracion global del proyecto.

Centraliza rutas, esquema de datos, hiperparametros de CV y URI de MLflow.
Cualquier modulo debe leer constantes desde aqui en vez de hardcodearlas.

Variables de entorno reconocidas (todas con fallback sano):
    MLFLOW_TRACKING_URI       : URI del tracking server (ej. http://10.0.1.42:5000).
                                Default: file:// en `mlruns/` local.
    MLFLOW_EXPERIMENT_PREFIX  : prefijo de experimentos MLflow (default 'productivity_').
                                El nombre real es prefix + variety.
    MODEL_REGISTRY_PREFIX     : prefijo del Model Registry (default 'productivity_').
    REPORT_PLOTLY_OFFLINE     : 1 = embeber plotly.js, 0 = CDN.
    S3_BUCKET                 : bucket S3 destino del raw / artifacts (usado por Taskfile).
    AWS_REGION                : default us-east-1.

Esquema de modelado (decidido tras EDA):
    Target          : KG/JR_H (kg cosechados por jornal-hora)
    Numericas (raw) : KG/HA, %INDUS, DPC, P/BAYA, HA
    Categoricas     : FORMATO, FUNDO
    Date-derived    : ANIO, MES_SIN/COS (orden 1-3), SEMANA_SIN/COS,
                      DIA_SEM_SIN/COS, TEMPORADA_ALTA/BAJA  (creadas en FeatureGenerator)

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

# `mlruns/` solo tiene sentido cuando el backend MLflow es file://. En EC2/prod
# el tracking server remoto guarda metadata en Postgres y artifacts en S3, asi
# que crear este dir produce ruido (queda vacio).
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
DEFAULT_VARIETIES: str = "POP"   # comma-separated; "all" expande a todas las hojas
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

# Perfiles de tuning (presupuesto de Optuna: cuantos trials, cuantos folds).
# NO confundir con entornos (local vs aws): el tuning es ortogonal al entorno.
#
# Tiempo estimado para 10k filas, 16 features, modelos XGB/LGB:
#   smoke   : ~1 min   (verificar que nada se rompe)
#   dev     : ~10 min  (iteracion durante desarrollo)
#   prod    : ~1.5 h   (modelo a promover)
#   prod_xl : ~2.5 h   (baseline contra el cual medir si mas trials mueven MAPE)
TUNING_PROFILES: dict[str, dict[str, int]] = {
    "smoke":   {"n_trials": 5,   "final_trials": 3,  "outer_folds": 2, "inner_folds": 2},
    "dev":     {"n_trials": 20,  "final_trials": 10, "outer_folds": 3, "inner_folds": 3},
    "prod":    {"n_trials": 60,  "final_trials": 30, "outer_folds": 5, "inner_folds": 3},
    "prod_xl": {"n_trials": 100, "final_trials": 50, "outer_folds": 5, "inner_folds": 3},
}
DEFAULT_TUNING: str = "dev"

# Defaults de CV (usados por tuning.py cuando el caller no override-a)
OUTER_CV_FOLDS: int = TUNING_PROFILES[DEFAULT_TUNING]["outer_folds"]
INNER_CV_FOLDS: int = TUNING_PROFILES[DEFAULT_TUNING]["inner_folds"]

# ---------------------------------------------------------------------------
# MLflow
# ---------------------------------------------------------------------------
# Si hay un MLFLOW_TRACKING_URI en el entorno (caso EC2 -> server remoto)
# se usa; si no, backend local file:// para desarrollo aislado.
MLFLOW_TRACKING_URI: str = _tracking_uri_raw or MLRUNS_DIR.as_uri()

# Prefijo del nombre de experimento. El experimento final por variedad sera
# `f"{MLFLOW_EXPERIMENT_PREFIX}{variety}"`.
# Default vacio: el nombre del experimento es la VARIEDAD (e.g. "POP").
# Esto crea UN experimento INDEPENDIENTE por variedad, y cada training
# es un run versionado (e.g. "xgb_v3") dentro de ese experimento.
MLFLOW_EXPERIMENT_PREFIX: str = os.environ.get("MLFLOW_EXPERIMENT_PREFIX", "")

# Prefijo del Model Registry (registered model = `f"{prefix}{variety}"`).
# Cada training de la misma variedad genera una nueva VERSION del mismo
# registered model.
# Default alineado con `backend_service` (que busca `f"{experiment_prefix}-{variety}"`
# con experiment_prefix="rnd-forest"). Cambiar aqui requiere cambiar tambien el
# `EXPERIMENT_PREFIX` del backend o no encontrara los modelos en el Registry.
MODEL_REGISTRY_PREFIX: str = os.environ.get("MODEL_REGISTRY_PREFIX", "rnd-forest-")

# ---------------------------------------------------------------------------
# Branding del reporte gerencial
# ---------------------------------------------------------------------------
REPORT_PROJECT_NAME: str = "Pronostico de productividad de cosecha (POP)"
REPORT_BUSINESS_UNIT: str = "Operaciones Agricolas"
REPORT_TARGET_LABEL: str = "kg por jornal-hora"

# Umbrales semaforo del reporte (sobre R2 mean del nested CV)
REPORT_R2_GOOD: float = 0.85   # >= verde / "Excelente"
REPORT_R2_WARN: float = 0.70   # >= amarillo / "Aceptable", < rojo / "Insuficiente"

# Targets gerenciales que se renderizan como gauges en el HTML.
# Mover la aguja por encima/debajo de estos valores cambia el color del gauge.
REPORT_R2_TARGET: float = 0.90   # R2 gerencial (KG/JR OOF) que el negocio quiere superar
REPORT_MAE_TARGET: float = 0.20  # MAE del modelo (KG/JR_H Test CV) que el negocio quiere NO superar

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
    "alta_confianza":      {"max_mape_pct": 15.0, "max_abs_gap": 0.10},
    "confianza_aceptable": {"max_mape_pct": 22.0, "max_abs_gap": 0.18},
    "confianza_limitada":  {"max_mape_pct": 35.0, "max_abs_gap": 0.30},
    # peor que confianza_limitada -> "no_recomendado"
}

# Subgroup MAPE multiplier sobre el global a partir del cual un FORMATO/FUNDO
# se marca como "problematico" en la seccion de Acciones Recomendadas.
REPORT_SUBGROUP_WARN_RATIO: float = 1.5   # >= 1.5x el MAPE global = warning

# Tamano minimo de un subgrupo (FORMATO o FUNDO) para que cuente como
# candidato a "problematico". Mas chico = ruido puro.
REPORT_SUBGROUP_MIN_N: int = 10

# Umbrales de tarjetas KPI ejecutivas (lenguaje natural). Cambiar aqui mueve
# tanto el HTML como el Excel sin tocar codigo.
KPI_PRECISION_HIGH_MAPE_PCT: float = 15.0    # MAPE <= 15 -> ALTO
KPI_PRECISION_MEDIUM_MAPE_PCT: float = 25.0  # MAPE <= 25 -> MEDIO, sino BAJO
KPI_R2_HIGH_PCT: float = 80.0                # R2*100 >= 80 -> ALTO
KPI_R2_MEDIUM_PCT: float = 60.0              # >= 60 -> MEDIO, sino BAJO
KPI_BASELINE_HIGH_IMPROVEMENT_PCT: float = 50.0
KPI_BASELINE_MEDIUM_IMPROVEMENT_PCT: float = 25.0

# Umbrales para acciones recomendadas auto-generadas.
ABS_GAP_WARN: float = 0.20      # |gap| > 0.20 = "memorizo entrenamiento"
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

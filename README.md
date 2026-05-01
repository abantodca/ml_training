# ml_training

Pipeline modular de regresión para pronosticar productividad de cosecha
(`KG/JR_H` — kg por jornal-hora) — soporta múltiples variedades en serie con
aislamiento de cache.

Implementado con **scikit-learn** (Pipeline + transformers `BaseEstimator`),
**Optuna** (TPE bayesiano multivariado) sobre Nested CV, **LightGBM** y
**XGBoost** como backends, **MLflow** (local o servidor remoto) para tracking
y un reporte HTML autocontenido para gerencia.

**Despliegue:** `Taskfile.yml` orquesta todo (data → S3 → infra → train →
fetch). **Infra:** Terraform provisiona VPC + 2 EC2 (MLflow t3.medium +
training t3.large) + S3 privado con versioning + IAM/SG estrictos.

---

## Quick start (local)

```bash
task setup                                                # instala deps
task data:split                                           # split por VARIEDAD
task smoke                                                # smoke (~1 min, POP)
task train:local TUNING=dev VARIETIES=POP,JUPITER         # multi-variety
task train:local TUNING=prod VARIETIES=all STAGE=Staging  # registra en Model Registry
mlflow ui --backend-store-uri file:./mlruns               # UI local
```

## Quick start (AWS, producción)

```bash
# 1) sube tu data al bucket
task deploy:upload-data

# 2) provisiona infra (lee infra/terraform.tfvars)
task infra:init && task infra:plan && task infra:apply
task infra:output                                         # copia el bloque para .env

# 3) sube el codigo y dispara training en la EC2
task deploy:upload-code
task train:remote TUNING=prod VARIETIES=POP,JUPITER

# 4) ve resultados en MLflow (artifacts viven en S3 via MLflow server)
task mlflow:open
```

## CLI directa (sin Taskfile)

```bash
# entrena UNA variedad
python main.py --tuning dev --varieties POP

# entrena VARIAS (cache se limpia entre cada una)
python main.py --tuning prod --varieties POP,JUPITER,VENTURA

# todas las hojas presentes en data/training/DB-HISTORICA.xlsx
python main.py --tuning prod --varieties all

# overrides finos
python main.py --tuning prod --n-trials 120 --final-trials 60   # ~75 min/variedad
python main.py --tuning prod --skip-final-tuning                # ahorra ~1/6

# registra en Model Registry (requiere MLflow remoto con backend DB)
python main.py --tuning prod --varieties POP --registry-stage Staging
```

### Tuning profiles preconfigurados (`src/config.py`)

| `--tuning` | n_trials | final_trials | outer × inner | trials totales | tiempo estimado* |
|---|---|---|---|---|---|
| `smoke` | 5 | 3 | 2 × 2 | 13 | ~1 min |
| `dev`   | 20 | 10 | 3 × 3 | 70 | ~6 min |
| `prod`  | 60 | 30 | 5 × 3 | 330 | ~30 min |
| `prod` + `--n-trials 120 --final-trials 60` | 120 | 60 | 5 × 3 | 660 | ~75 min |
| `prod` + `--n-trials 200 --final-trials 100` | 200 | 100 | 5 × 3 | 1100 | ~2 h |

\* sobre 10 073 filas y este hardware. Calibrar con un `--tuning dev` previo si la máquina cambia.

### Salidas
- `artifacts/final_pipeline_<modelo>.joblib` — pipeline (preprocesador + modelo) listo para `joblib.load(...)` y `predict(...)` en producción.
- `artifacts/run_summary_<modelo>.json` — métricas, hiperparámetros, paths y duración (consumo CI / dashboards).
- `reports/reporte_modelo_YYYYMMDD_HHMMSS.html` — reporte gerencial autocontenido.
- `mlruns/` — backend local de MLflow (todo lo anterior se loguea también ahí).
- `logs/pipeline_run.log` — bitácora con `archivo:línea:función`.

---

## Mapa de la arquitectura

```
ml_training/
├── main.py                                 # Orquestador (multi-variety + cache cleanup)
├── Taskfile.yml                            # tareas deploy / data / train / mlflow / infra
├── .env.example                            # variables del entorno (S3, MLflow, SSH)
├── infra/                                  # Terraform modular
│   ├── main.tf                             # compone modulos
│   ├── versions.tf, variables.tf, outputs.tf, terraform.tfvars.example
│   ├── cloud-init/{mlflow_server,training_node}.sh
│   └── modules/{network,storage,security,iam,ec2_instance,lambda_power}/
├── scripts/
│   ├── prepare_data.py                     # split del acumulado por VARIEDAD
│   └── sh/                                 # toda la logica del Taskfile (19 scripts .sh)
├── src/
│   ├── config.py                           # Esquema, rutas, seeds, MLflow URI, branding
│   ├── utils/logger.py                     # setup_logging() archivo + consola
│   ├── step_01_load/data_loader.py         # load_data() → (X_raw, y) con validación
│   ├── step_02_clean/
│   │   ├── imputers.py                     # CustomKNNImputer (KNN + median fallback)
│   │   └── outliers.py                     # OutlierCapper (iqr / percentile)
│   ├── step_03_features/feature_engineering.py  # FeatureGenerator (cíclicas + one-hot)
│   ├── pipeline/build_pipeline.py          # imputer → outliers → feature_engineering
│   ├── step_04_train/
│   │   ├── model_xgb.py                    # get_xgb_model()
│   │   ├── model_lgb.py                    # get_lgb_model()
│   │   ├── search_spaces.py                # espacios Optuna por backend (registry extensible)
│   │   └── tuning.py                       # perform_nested_cv() Optuna TPE multivariado
│   ├── step_05_evaluate/                   # métricas + diagnósticos + reporte
│   │   ├── metrics.py                      # MAE, RMSE, R², MAPE
│   │   ├── diagnostics.py                  # gráficos matplotlib → base64 PNG
│   │   └── html_generator.py               # reporte gerencial autocontenido
│   └── step_06_track/                      # tracking experimental
│       └── mlflow_registry.py              # init / log_metrics / log_params / log_pipeline
├── data/                                   # raw + training (no en git)
├── logs/, artifacts/, reports/, mlruns/    # creados automáticamente
└── requirements.txt
```

---

## Flujo del pipeline

```
main.main()
└── 1. data_loader.load_data()          # lee Excel, valida, elimina leakage/inútiles
└── 2. build_pipeline.create_preprocessing_pipeline()
        ├── CustomKNNImputer           # KNN sobre numéricas (median fallback si >30% missing)
        ├── OutlierCapper              # IQR o percentil (tuneable)
        └── FeatureGenerator           # cíclicas (sin/cos) + one-hot, drop FECHA
└── 3. tuning.perform_nested_cv()
        ├── Outer KFold (k=5)          # mide generalización del PROCEDIMIENTO completo
        │   └── Inner KFold (k=3)      # cross_val_score de cada trial
        │       └── Optuna TPE         # busca preprocesador + modelo conjuntamente
        └── refit final con todo el dataset y los best_params
└── 4. step_05_evaluate.html_generator   # reporte gerencial (KPIs + gráficos embebidos)
└── 5. step_06_track.mlflow_registry     # log de params, métricas y pipeline a MLflow
└── 6. joblib.dump(pipeline, ...)        # promoción a producción
```

---

## Decisiones técnicas (con respaldo estadístico)

### Esquema de features
Tras EDA sobre 10 073 filas:

| Columna | Decisión | Justificación |
|---|---|---|
| `KG/JR`, `H-EF` | **excluir** | leakage — `target = KG/JR ÷ H-EF` exactamente (max\_abs\_diff = 0). |
| `VARIEDAD` | excluir | un único valor (`POP`). |
| `CALIBRADO` | excluir | un único valor (`NO`). |
| `DIA_SEM` | excluir | mutual\_info ≈ 0 vs target. |
| `MES` (raw) | reemplazar | usado solo como insumo de `MES_SIN/COS` (cíclica). |
| `KG/HA` | mantener | top MI = 0.48 (driver dominante). |
| `%INDUS` | mantener | MI = 0.29. |
| `DPC` | mantener | MI = 0.22. |
| `P/BAYA` | mantener | MI = 0.13 (KNN-impute, 39% missing). |
| `HA` | mantener | MI = 0.08. |
| `FORMATO` | one-hot | mean: 1.76 (CLAMSHELL 6 OZ) → 5.17 (GRANEL). |
| `FUNDO` | one-hot | 4 niveles, mean LN = 2.02 vs A9 = 5.36 (2.6×). |
| `FECHA` | derivar cíclicas + año | estacionalidad fuerte (abril 2.3 → septiembre 6.7) y deriva temporal entre años. |

### Tuning
- **Optuna TPE multivariado** (acelera convergencia explotando correlaciones entre params) + **MedianPruner** (corta trials notoriamente malos).
- El **preprocesador también se tunea**: `imputer__n_neighbors`, `outliers__method`, `outliers__factor`. El pipeline completo es el espacio de búsqueda.
- **Nested CV**: outer (k=5) mide generalización del procedimiento completo (preproc + tuning + entrenamiento), inner (k=3) selecciona hiperparámetros sin contaminar el outer test.

### Reporte gerencial
Una sola página HTML autocontenida:
- Header con marca, unidad de negocio, modelo, fecha.
- Resumen ejecutivo con badge semáforo (Excelente / Aceptable / Insuficiente según R²).
- 4 KPIs (R², MAE, MAE std, estado) con código de color.
- Gráficos embebidos (PNG base64): predicho vs real OOF, residuales, importancia de variables.
- Desempeño por subgrupo (FORMATO, FUNDO).
- Tabla de hiperparámetros óptimos y representación técnica del pipeline.

Las predicciones que alimentan los gráficos son **out-of-fold** (cada predicción proviene de un modelo que NO observó esa fila), no in-sample.

---

## Convención de nombres

Los módulos `step_XX_verbo/` codifican el orden del pipeline en el propio
nombre — el lector entiende la secuencia sin abrir un diagrama. Se mantienen
así por:

1. **Determinismo visual**: `01_load → 02_clean → 03_features → 04_train → 05_evaluate → 06_track` es legible sin contexto.
2. **Compatibilidad Python**: módulos no pueden empezar con dígitos puros (`01_load` falla); el prefijo `step_` lo resuelve.
3. **Estabilidad de imports**: renombrar implica tocar todos los `from src.step_X import ...` y los pkl ya serializados (que recuerdan el path del pipeline). El costo supera al beneficio.

---

## Reproducibilidad
- `RANDOM_STATE = 42` propagado a `KFold`, `TPESampler`, `XGBRegressor`, `LGBMRegressor`.
- Cada run de MLflow guarda parámetros del pipeline completo (incluyendo del preprocesador) y el pipeline serializado.
- El log incluye `archivo:línea:función` para localizar fallos en segundos.

---

## Auditoría / hardening aplicado

| Categoría | Mejora |
|---|---|
| Overfitting | Cada outer fold reporta `MAE_train`, `MAE_test`, `gap`. El reporte HTML agrega una sección "Análisis de overfitting" con verdict (verde/amarillo/rojo) según gap relativo (<10% / 10-30% / >30%). |
| MLflow run | Tags clave (`r2_mean`, `mae_test_mean`, `mae_train_mean`, `overfit_gap`) → filtros directos en la UI. |
| MLflow artifacts | `best_params_<variedad>_<modelo>.json` se sube como artifact en `hyperparameters/` (evita el truncado a 250 chars que aplican los `mlflow.log_params`). |
| MLflow signature | `infer_signature` con muestra de X/y → modelo autodescribible para `mlflow models serve`. |
| Cache cleanup | Entre variedades: `gc.collect()`, `plt.close('all')`, `mlflow.end_run()` defensivo. |
| Skip-final-tuning | Cuando se omite la ronda final, ahora usa el `best_params` del **mejor** outer fold (argmin MAE_test), no el último. |
| Infra estable | **Elastic IPs** en ambas EC2 → DNS no cambia tras stop/start. |
| Costo S3 | Lifecycle: versiones viejas de `mlflow-artifacts/` expiran a 30d, `code/` a 14d, multipart abortados a 7d. |
| Encriptación | EBS y S3 con AES256, S3 con `public_access_block` total. |
| Venv en remoto | `.env` de la EC2 expone `PYTHON=/opt/.../venv/bin/python`; el Taskfile usa `{{.PYTHON}}` → `task` corre con el intérprete correcto incluso vía SSH no-interactivo. |
| Outputs Terraform | Bloque `env_block_for_dotenv` listo para pegar — un solo paso para conectar tu `.env` local con la infra recién provisionada. |

# ml_training

Pipeline modular de regresión para pronosticar productividad de cosecha
(`KG/JR_H` — kg por jornal-hora) — soporta **múltiples variedades** (en serie
o en paralelo) y **múltiples backends** (XGB y LGB) con selección automática
del campeón por variedad.

Implementado con **scikit-learn** (Pipeline + transformers `BaseEstimator`),
**Optuna** (TPE bayesiano multivariado) sobre **Nested CV**, **LightGBM** y
**XGBoost** como backends entrenados en paralelo, **MLflow** (local o
servidor remoto) para tracking + Model Registry, y un **dashboard HTML
ejecutivo** autocontenido por variedad.

**Entorno:** local-only (Windows / Linux). MLflow `file://mlruns/` por
default. `Taskfile.yml` orquesta data split, smoke, training, MLflow UI
local, logs y auditoría entre runs.

> **Nota sobre el nombre del repo:** se llama `ml_random_forest` por razones
> históricas. El pipeline actual entrena **XGBoost + LightGBM** (no Random
> Forest) y selecciona el mejor por variedad.

---

## Quick start (local)

```bash
task setup                                                # instala deps
task data:split                                           # split por VARIEDAD
task smoke                                                # smoke (~1-2 min, POP, XGB+LGB)
task train:local VARIETIES=POP,JUPITER                    # multi-variety
task train:local VARIETIES=all PARALLEL=4 TUNING=prod     # paralelo
task train:local VARIETIES=POP STACKING=gam               # con meta GAM
task mlflow:ui                                            # UI MLflow en :5000
task audit:compare -- --variety POP --last 5              # comparativa runs
```

## CLI directa (sin Taskfile)

```bash
# default: --model auto -> entrena XGB y LGB y elige campeón por variedad
python main.py --tuning dev --varieties POP

# fuerza UN solo backend (~50% más rápido, sin comparación)
python main.py --tuning prod --varieties POP --model xgb

# multi-variety (cache se limpia entre variedades)
python main.py --tuning prod --varieties POP,JUPITER,VENTURA

# todas las hojas presentes en data/training/DB-HISTORICA.xlsx
python main.py --tuning prod --varieties all

# multi-variety EN PARALELO (procesos independientes; auto-ajusta inner_cv_n_jobs)
python main.py --tuning prod --varieties all --parallel-varieties 4

# overrides finos del presupuesto Optuna
python main.py --tuning prod --n-trials 120 --final-trials 60
python main.py --tuning prod --skip-final-tuning           # ahorra ~1/(outer+1)

# registra campeón en Model Registry (requiere MLflow remoto con backend DB)
python main.py --tuning prod --varieties POP --registry-stage Staging
python main.py --tuning prod --varieties POP --no-register # desactiva el registry

# stacking opt-in: envuelve el campeón en GAM como meta-learner
python main.py --tuning dev --varieties POP --stacking gam
```

### Tuning profiles (`src/config.py`)

| `--tuning` | n_trials | final_trials | outer × inner | trials totales | tiempo estimado* |
|---|---|---|---|---|---|
| `smoke`   | 5   | 3  | 2 × 2 | 13   | ~1 min   |
| `dev`     | 20  | 10 | 3 × 3 | 70   | ~10 min  |
| `prod`    | 60  | 30 | 5 × 3 | 330  | ~1.5 h   |
| `prod_xl` | 100 | 50 | 5 × 3 | 550  | ~2.5 h   |

\* sobre 10 073 filas, **POR BACKEND**. Con `--model auto` (default) se duplica.
Con `--parallel-varieties N` el wallclock por job se acerca a `total / N`
si hay cores suficientes.

### Selección de campeón (`src/step_05_evaluate/champion.py`)

Cuando se entrenan XGB y LGB para la misma variedad, `select_champion`
decide con un **lex-order estricto** (no un score combinado):

1. **Generalización** — menor `|gap|` entre MAE_train y MAE_test (anti-overfit).
2. **Estabilidad en data total** — menor MAPE de negocio (KG/JR sobre el dataset completo, in-sample).
3. **Eficiencia** — menor `elapsed_seconds` ante empate técnico.

Empates "blandos" usan tolerancias (`GAP_TIE_TOLERANCE=0.005`, `FULL_MAPE_TIE_TOLERANCE=0.5pp`)
para que ruido de CV no haga inestable la decisión. La justificación textual
de por qué ganó el campeón se persiste en `champion_summary["justification"]`.

`composite_score` aún se computa y loguea como tag de MLflow para compatibilidad
con dashboards históricos, pero **no** participa en la decisión.

### Salidas

Versionadas por run (`xgb_v3`, `lgb_v7`, …) para conservar historial sin
sobrescribir entre re-entrenamientos:

- `artifacts/final_pipeline_<variety>_<run_name>.joblib` — pipeline (preprocesador + `OOFEnsembleRegressor`) listo para `joblib.load(...)` y `predict(...)`.
- `artifacts/run_summary_<variety>_<run_name>.json` — métricas, hiperparámetros, paths, duración.
- `artifacts/best_params_<variety>_<run_name>.json` — hiperparámetros sin truncado de MLflow.
- `artifacts/run_summary_AGGREGATE.json` — resumen multi-variety: campeones, fallos, tiempo total.
- `reports/Winner_<variety>.html` — dashboard ejecutivo autocontenido (solo para el campeón).
- `reports/Winner_<variety>.xlsx` — Excel multi-hoja con métricas y predicciones (solo campeón).
- `mlruns/` — backend local de MLflow (run versionado por modelo dentro del experimento de la variedad).
- `logs/pipeline_run.log`, `logs/variety_<variety>.log`, `logs/business_audit.jsonl`.

---

## Mapa de la arquitectura

```
ml_training/
├── main.py                                 # Entrypoint thin (parse + delega en orchestration/)
├── Taskfile.yml                            # Tareas locales: setup / data / train / mlflow:ui / logs
├── .env.example                            # Variables de entorno opcionales
├── requirements.txt, requirements-dev.txt
├── scripts/
│   ├── prepare_data.py                     # split del acumulado por VARIEDAD
│   ├── audit_compare.py                    # comparativa entre runs (lee logs/business_audit.jsonl)
│   ├── clean_artifacts.py                  # GC de artifacts/ viejos (KEEP por variety+model)
│   └── sh/                                  # scripts .sh con la lógica del Taskfile
└── src/
    ├── config.py                            # Esquema, rutas, seeds, MLflow URI, branding del reporte
    ├── utils/logger.py                      # setup_logging() archivo + consola
    ├── step_01_load/data_loader.py          # load_data() → (X_raw, y) con validación + leakage check
    ├── step_02_clean/
    │   ├── missing_flags.py                 # MissingFlagger (boolean per-feature)
    │   ├── imputers.py                      # CustomKNNImputer (KNN + median fallback)
    │   ├── outliers.py                      # OutlierCapper (iqr / percentile, tuneable)
    │   └── _helpers.py
    ├── step_03_features/
    │   ├── feature_engineering.py           # FeatureGenerator (cíclicas + ratios estructurales + one-hot)
    │   └── lag_features.py                  # add_lag_features (~31 features rolling/seasonal/ratios) — invocado desde data_loader.py
    ├── pipeline/build_pipeline.py           # missing_flags → imputer → outliers → features → variance_filter
    ├── step_04_train/
    │   ├── model_xgb.py                     # get_xgb_model() — XGBRegressor envuelto en TTR
    │   ├── model_lgb.py                     # get_lgb_model() — LGBMRegressor (objective=quantile α=0.5)
    │   ├── model_gam.py                     # get_gam_meta_model() — LinearGAM (pyGAM) para stacking
    │   ├── search_spaces.py                 # Espacios Optuna por backend + meta (registries extensibles)
    │   ├── target_transform.py              # log1p + cap p99.5 vía TransformedTargetRegressor (CV-safe)
    │   ├── oof_ensemble.py                  # K refits sobre folds; predict promedia las K
    │   ├── stacking.py                      # StackedRegressor — campeón → GAM (opt-in via --stacking gam)
    │   └── tuning.py                        # perform_nested_cv() + sample_weights + CV adaptativo + stacking_meta
    ├── step_05_evaluate/
    │   ├── metrics.py                       # MAE, RMSE, R², MAPE
    │   ├── diagnostics.py                   # gráficos matplotlib → base64 PNG
    │   ├── champion.py                      # select_champion (lex-order: gap → MAPE → tiempo)
    │   ├── explainability.py                # feature importance + SHAP del campeón
    │   └── html/                            # dashboard ejecutivo modular
    │       ├── winner_dashboard.py          # entrypoint: Winner_<variety>.html
    │       ├── sections.py, technical.py    # KPIs, tarjetas, tablas, sección técnica
    │       ├── styles.py, helpers.py        # CSS embebido + utilidades
    │       └── __init__.py
    ├── step_06_track/
    │   ├── mlflow_registry.py               # init / log_metrics / log_params / log_pipeline / Model Registry
    │   ├── business_validation.py           # KG/JR (= KG/JR_H × H-EF) OOF + in-sample
    │   └── business_export.py               # Winner_<variety>.xlsx multi-hoja
    └── orchestration/
        ├── cli.py                           # argparse + resolve_models / resolve_varieties / resolve_settings
        ├── single_run.py                    # entrena UN modelo para UNA variedad → ModelResult
        ├── variety_runner.py                # orquesta multi-modelo + select_champion + render dashboard
        ├── runners.py                       # run_sequential / run_parallel (procesos independientes)
        └── cleanup.py                       # gc.collect + plt.close + mlflow.end_run defensivo
```

---

## Flujo del pipeline

```
main.main()
└── parse_args + resolve_models (auto | xgb | lgb | xgb,lgb | all)
└── runners.run_sequential | run_parallel
        └── variety_runner.run_variety(variety)
                ├── 1. data_loader.load_data(sheet=variety)
                │       ├── lee Excel, valida, drop leakage/inútiles, group-rare en FORMATO
                │       └── add_lag_features → ~31 features rolling/seasonal/ratios pre-CV
                ├── 2. build_pipeline.create_preprocessing_pipeline()
                │       ├── MissingFlagger
                │       ├── CustomKNNImputer       (KNN + median fallback si >30% missing)
                │       ├── OutlierCapper           (iqr | percentile, factor tuneable)
                │       ├── FeatureGenerator       (ratios estructurales + cíclicas sin/cos + one-hot, drop FECHA)
                │       └── VarianceThreshold      (descarta dummies constantes en la variedad)
                ├── 3. PARA CADA modelo en {xgb, lgb}:
                │       └── single_run.train_model(variety, model_type)
                │               └── tuning.perform_nested_cv()
                │                       ├── CV adaptativo: StratifiedKFold por
                │                       │   FUNDO_FORMATO (cascada FUNDO → FORMATO →
                │                       │   KFold) con colapso de clases raras a 'RARE'
                │                       ├── sample_weights ∝ 1/freq por bins de y
                │                       │   (igual ancho, cap=5×, normalizados a media=1)
                │                       ├── Outer CV (k=5)  → MAE_test, MAE_train, gap, R²
                │                       │   └── Inner CV (k=3) + Optuna TPE multivariado
                │                       │       sobre preprocessor params + model params
                │                       ├── Ronda final sobre dataset completo (final_trials)
                │                       └── refit final con OOFEnsembleRegressor
                │                           (K=5 pipelines sobre folds; predict promedia)
                ├── 4. champion.select_champion([result_xgb, result_lgb])
                │       └── lex-order: gap → MAPE_total → tiempo
                ├── 5. business_validation: KG/JR = KG/JR_H × H-EF (OOF + in-sample)
                ├── 6. mlflow_registry: log metrics + params + pipeline + signature
                │       └── promueve campeón al Model Registry si --registry-stage
                └── 7. winner_dashboard + business_export → Winner_<variety>.html / .xlsx
```

---

## Decisiones técnicas (con respaldo estadístico)

### Esquema de features

Tras EDA sobre 10 073 filas:

| Columna | Decisión | Justificación |
|---|---|---|
| `KG/JR`, `H-EF` | **excluir** | leakage — `target = KG/JR ÷ H-EF` exactamente (max\_abs\_diff = 0). |
| `VARIEDAD` | excluir | un único valor por hoja (split por variedad). |
| `CALIBRADO`, `DIA_SEM`, `MES` (raw) | excluir | mutual_info ≈ 0 vs target o reemplazadas por sus cíclicas. |
| `KG/HA` | mantener | top MI = 0.48 (driver dominante). |
| `%INDUS` | mantener | MI = 0.29. |
| `DPC` | mantener | MI = 0.22. |
| `P/BAYA` | mantener | MI = 0.13 (KNN-impute, ~39% missing). |
| `HA` | mantener | MI = 0.08. |
| `DIA_COSECHA` | mantener | día desde el inicio de la cosecha (capta deriva intra-temporada). |
| `FORMATO` | one-hot + group-rare | colapsa categorías con n<50 a `OTROS` (`RARE_MIN_COUNT`). |
| `FUNDO` | one-hot | mean LN ≈ 2.02 vs A9 ≈ 5.36 (≈ 2.6×). |
| `FECHA` | derivar cíclicas + año | estacionalidad fuerte (abril 2.3 → septiembre 6.7) y deriva temporal entre años. |

### Features derivadas (lag + estructurales)

Además de las 6 numéricas raw, 2 categóricas one-hot y 13 cíclicas/temporales,
el pipeline genera dos bloques de features derivadas:

**Lag features** (`step_03_features/lag_features.py`, ~31 columnas, calculadas en `data_loader.py` antes del CV split):

- **Rolling por grupo × variable × ventana** (24 cols): mediana de las N obs anteriores con `shift(1)` para excluir la fila actual.
  - Grupos: `FUNDO+FORMATO` (FF), `FUNDO` (F), `FORMATO` (FMT) — cascada de densidad.
  - Variables: `KG_JR_H` (target) y `KG/HA`.
  - Ventanas: 7d, 14d, 30d, 90d.
- **Lag estacional anual** (2 cols): mediana en ventana `fecha − 365d ± 15d` por (FUNDO, FORMATO). Captura ciclo agronómico que `90d` no ve.
- **Ratios temporales** (3 cols):
  - `KG_HA_ratio_FF_30/90 = KG/HA actual / KG_HA_lag_FF_30(o 90)` — desempeño relativo al histórico.
  - `delta_KG_JR_H_30_90 = lag_30 / lag_90` — aceleración / deceleración del target.
- **Cold-start flags** (2 cols): `LAG_FF_COLD`, `LAG_FF_SEASONAL_COLD` con sentinel `-1` para grupos sin historia. Los árboles aíslan estas filas como hojas distintas.

> Los lags `KG_JR_H_lag_*` cumplen la función de un target encoding pero **temporal,
> multi-window y multi-grupo** (con fallback FF → F → FMT por densidad). Por eso un
> mean encoding plano sobre FUNDO/FORMATO sería redundante y conceptualmente inferior.

**Ratios estructurales intra-fila** (`step_03_features/feature_engineering.py`, 4 columnas, calculados dentro del Pipeline después del imputer y outlier capping):

| Feature | Fórmula | Captura |
|---|---|---|
| `KG_TOTAL` | `KG/HA × HA` | Kilos absolutos cosechados (escala/tamaño de la parcela). |
| `INDUS_KG_HA` | `%INDUS × KG/HA` | Kg de calidad industrial por hectárea. |
| `KG_PER_BAYA` | `KG/HA ÷ P/BAYA` | Eficiencia volumétrica (cuánto pesa el agregado). |
| `KG_HA_PER_DPC` | `KG/HA ÷ DPC` | Velocidad de cosecha por unidad DPC. |

Las divisiones usan `np.where(den > 0, num/den, NaN)`; los NaN resultantes son tratados nativamente por XGB y LGB (rama default elegida por loss en el split). No tocan target ni `H-EF`, así que no requieren CV-fold awareness.

### Tuning

- **Optuna TPE multivariado** (acelera convergencia explotando correlaciones entre params); sin pruner porque cada trial reporta UN solo score (CV ya hecho), no valores intermedios.
- **El preprocesador también se tunea**: `imputer__n_neighbors`, `outliers__method`, `outliers__factor`. El espacio de búsqueda es el pipeline completo, no solo el modelo.
- **Search spaces aislados por backend** (`search_spaces.py`): registry extensible — añadir un backend implica un solo archivo, sin tocar `tuning.py`.

### Nested CV adaptativo

- **Outer (k=5)** mide generalización del PROCEDIMIENTO completo (preproc + tuning + entrenamiento). **Inner (k=3)** selecciona hiperparámetros sin contaminar el outer test.
- **Estratificación adaptativa por variedad**: cascada `FUNDO_FORMATO` → `FUNDO` → `FORMATO` → `KFold` simple. En cada nivel se valida que tras colapsar clases con `n<min_count` a `RARE` queden ≥2 clases distintas con tamaño suficiente para el inner.
- `min_count = max(outer, ceil(inner × outer / (outer−1)))` — garantiza que el inner Stratified tenga ≥`inner_folds` por clase tras el split del outer.

### Anti-overfitting / estabilidad

- **`TransformedTargetRegressor`** (`target_transform.py`): aplica `log1p(min(y, p99.5))` en el espacio de y antes de fittear el modelo y `expm1` al predecir. CV-safe porque el cap se calcula DENTRO de cada fold sobre `y_train`. Estabiliza varianza y aplasta ~0.5% de outliers extremos sin tocar `y_test` del scoring.
- **Sample weights** (`tuning.compute_sample_weights`): pesos inversos a la densidad del target con bins de IGUAL ANCHO (no qcut), cap=5×, normalizados a media=1. Compensa el sesgo "regresión a la media" de los árboles dando más peso a deciles raros (target alto/bajo).
- **`OOFEnsembleRegressor`** (`oof_ensemble.py`): refit final = K=5 pipelines clonados, cada uno entrenado en `(K−1)/K` del dataset según un `KFold`; `predict()` promedia las K predicciones. Reduce varianza ~5–10% del modelo de producción a costa de 5× el tiempo del refit final (despreciable vs nested CV). `K=1` degenera al modo legacy bit-for-bit.

### Stacking (capa GAM, opt-in)

Activado con `--stacking gam` (default `none` → comportamiento bit-for-bit idéntico al actual). Envuelve el campeón XGB/LGB en `StackedRegressor`, que en cascada hace:

```
fit(X, y):
    1. KFold(STACKING_OOF_FOLDS) sobre X → oof_pred (predicciones honestas del base)
    2. meta_features = [oof_pred, X[STACKING_X_SUBSET] imputado por mediana]
    3. LinearGAM (pyGAM) fit(meta_features, y, weights=sample_weight)
    4. base.fit(X, y) sobre TODO X (lo que se usa en inferencia)

predict(X):
    pred_base = base.predict(X)
    return gam.predict([pred_base, X[STACKING_X_SUBSET] imputado])
```

**Por qué GAM y no Ridge / red neuronal**: con 3-4 features de entrada al meta y N≈10k filas, el GAM aporta no-linealidad suave por término (`s(pred_base) + s(KG/HA) + s(%INDUS) + s(DIA_COSECHA)`) que los árboles aproximan con escalones; un ridge sería demasiado rígido y una red neuronal sufriría con N tan chico. La interpretabilidad de los splines es un bonus para el dashboard.

**Por qué SOLO un subset de features al meta**: pasarle al GAM las ~50 columnas que ve el base (raw + lags + ratios + dummies) sería curse-of-dim — el GAM diverge. El subset por default (`config.STACKING_X_SUBSET = ["KG/HA", "%INDUS", "DIA_COSECHA"]`) son las 3 features continuas con MI más alta y efectos típicamente suaves no capturados por splits.

**Por qué los lags `KG_JR_H_lag_*` no van al meta**: ya cumplen función de target encoding temporal multi-window dentro del base. Pasarlos al GAM además duplicaría señal y complicaría el OOF del meta (riesgo de leakage cruzado entre el lag CV y el stacking CV).

**OOF doble + paridad**: el nested CV (outer 5 × inner 3) sigue midiendo el **base puro**. Las métricas `nested_cv_mae_mean`, `nested_cv_gap_mean`, `nested_cv_r2_mean` quedan comparables con runs históricos. La capa GAM solo afecta el `final_pipeline.predict()` y la `business_validation` (KG/JR refit + predict all). Si querés evaluar el stacking de forma honesta, comparalo via `task audit:compare` filtrando por `tag.stacked=true` en MLflow.

**Backend**: cero cambios. `mlflow.pyfunc.load_model("rnd-forest-{variety}").predict(X_raw)` carga el `StackedRegressor` y la cascada base→meta es interna al wrapper. La signature de MLflow es la misma porque las features de entrada son idénticas.

**MLflow tags**: cada run loguea `stacked=true|false` y `meta_model=gam` cuando aplica. Filtrable directo en la UI.

### Validación en unidad de negocio

`KG/JR_H` (kg/jornal-hora) es la unidad del modelo, pero la unidad
gerencial es `KG/JR` (kg/jornal). `business_validation.py` recompone
`KG/JR = predicción × H-EF` y reporta MAPE/R² en esa unidad, tanto
**OOF** (honesto, para el dashboard) como **in-sample** (sanity check del
modelo de producción aplicado a toda la historia).

### Reporte gerencial (`Winner_<variety>.html`)

Una sola página HTML autocontenida (Plotly inline o por CDN según
`REPORT_PLOTLY_OFFLINE`):

- Header con marca, unidad de negocio, descripción del modelo, fecha.
- Resumen ejecutivo con badge semáforo (Excelente / Aceptable / Insuficiente / No recomendado) según R² OOF + brecha train-test.
- Gauges de R² y MAE contra targets gerenciales (`REPORT_R2_TARGET`, `REPORT_MAE_TARGET`).
- 4 KPIs con código de color (precisión, R², mejora vs baseline, estado).
- Gráficos embebidos: predicho vs real OOF, residuales, feature importance, SHAP.
- Desempeño por subgrupo (FORMATO, FUNDO) con flag automático de subgrupos problemáticos (`MAPE > 1.5 × MAPE_global` y `n ≥ REPORT_SUBGROUP_MIN_N`).
- Análisis de overfitting (gap relativo, verdict).
- Tabla de hiperparámetros óptimos + representación técnica del pipeline.
- Bloque de "Aplicación Total" (modelo refit aplicado a toda la historia, in-sample).

Las predicciones honestas que alimentan los gráficos son **out-of-fold**:
cada predicción proviene de un modelo que NO observó esa fila durante
el entrenamiento.

---

## Convención de nombres

Los módulos `step_XX_verbo/` codifican el orden del pipeline en el propio
nombre — el lector entiende la secuencia sin abrir un diagrama. Se mantienen
así por:

1. **Determinismo visual**: `01_load → 02_clean → 03_features → 04_train → 05_evaluate → 06_track` es legible sin contexto.
2. **Compatibilidad Python**: módulos no pueden empezar con dígitos puros (`01_load` falla); el prefijo `step_` lo resuelve.
3. **Estabilidad de imports**: renombrar implica tocar todos los `from src.step_X import ...` y los `.joblib` ya serializados (que recuerdan el path del pipeline). El costo supera al beneficio.

---

## Reproducibilidad

- `RANDOM_STATE = 42` propagado a `KFold`, `StratifiedKFold`, `TPESampler`, `OOFEnsembleRegressor`, `XGBRegressor`, `LGBMRegressor`.
- Cada run de MLflow guarda parámetros del pipeline completo (modelo + preprocesador), métricas Train/Test/Full, y el pipeline serializado con `infer_signature` (modelo autodescribible para `mlflow models serve`).
- `best_params_<variety>_<run_name>.json` se sube como artifact en `hyperparameters/` para evitar el truncado a 250 chars de `mlflow.log_params`.
- El log incluye `archivo:línea:función` para localizar fallos en segundos.
- `logs/business_audit.jsonl` registra cada run con métricas de negocio para auditoría histórica (`task audit:compare`).

---

## MLflow: experimentos y Model Registry

- **Un experimento por variedad**: `MLFLOW_EXPERIMENT_PREFIX + variety`. Default prefix vacío → el experimento es el nombre de la variedad (`POP`, `JUPITER`, …).
- **Run versionado dentro del experimento**: `xgb_v1`, `xgb_v2`, …, `lgb_v1`, … (`next_run_version` autoincrementa por modelo). El campeón histórico vive en el mismo experimento que sus rivales y se distingue por sus tags.
- **Model Registry**: `MODEL_REGISTRY_PREFIX + variety` (default `rnd-forest-POP`, `rnd-forest-JUPITER`, …). Cada training del campeón crea una nueva versión del registered model. Promoción a `Staging` / `Production` opt-in vía `--registry-stage`.

> El `MODEL_REGISTRY_PREFIX` por default está alineado con el `backend_service`,
> que busca modelos como `f"{experiment_prefix}-{variety}"` con
> `experiment_prefix="rnd-forest"`. Cambiarlo aquí requiere cambiarlo también
> en el backend o no encontrará los modelos.

---

## Auditoría / hardening aplicado

| Categoría | Mejora |
|---|---|
| Overfitting | Cada outer fold reporta `MAE_train`, `MAE_test`, `gap`. El reporte HTML agrega "Análisis de overfitting" con verdict (verde/amarillo/rojo) según gap. El selector de campeón usa `|gap|` como **primer** criterio. |
| Selección multi-modelo | `select_champion` con lex-order estricto + tolerancias por bucket. Justificación textual auto-generada. |
| Stacking opcional | `--stacking gam` envuelve el campeón en `StackedRegressor` con pyGAM como meta. OOF interno via `cross_val_predict` (K=5). Default off → paridad bit-for-bit. Backend sin cambios. |
| Estabilidad varianza | `TransformedTargetRegressor` (log1p + cap p99.5 CV-safe) + `OOFEnsembleRegressor` (K=5 refits promediados). |
| Compensación cola | `compute_sample_weights` por bins de igual ancho del target (cap=5×). |
| MLflow run | Tags clave (`r2_mean`, `mae_test_mean`, `mae_train_mean`, `overfit_gap`, `composite_score`) → filtros directos en la UI. |
| MLflow artifacts | `best_params_*.json` versionado por run_name (sin overwrite entre re-entrenamientos). |
| MLflow signature | `infer_signature` con muestra de X/y; ints casteados a float64 para no romper schema enforcement con NaN en inferencia. |
| Multi-variety | `--parallel-varieties N`: procesos independientes con cache cleanup natural; auto-ajusta `inner_cv_n_jobs = cores // N` para evitar oversubscription. |
| Cache cleanup | Entre variedades: `gc.collect()`, `plt.close('all')`, `mlflow.end_run()` defensivo (`orchestration/cleanup.py`). |
| Skip-final-tuning | Cuando se omite la ronda final, usa el `best_params` del **mejor** outer fold (argmin MAE_test), no el último. |
| Stratified adaptativo | Cascada `FUNDO_FORMATO → FUNDO → FORMATO → KFold` con colapso de clases raras a `RARE`. |
| Group-rare | `RARE_MIN_COUNT=50` en FORMATO: categorías con `n<50` se colapsan a `OTROS` antes del one-hot. |
| Auditoría JSONL | `logs/business_audit.jsonl` (1 línea por run); `task audit:compare -- --variety POP --last 5` para comparativas. |
| Cleanup artifacts | `task clean:artifacts KEEP=10` conserva los últimos N runs por variety+model. |
| Venv portátil | `.env` (opcional) puede exponer `PYTHON=/path/a/venv/bin/python`; el Taskfile usa `{{.PY}}` → corre con el intérprete correcto sin necesidad de activar el venv. |

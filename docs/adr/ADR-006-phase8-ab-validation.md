# ADR-006: Validación A/B del pipeline post-EDA

- **Estado**: Proposed (pending live execution)
- **Fecha**: 2026-05-09
- **Tags**: `validation`, `ab-test`, `eda-driven`

## Contexto

Las Fases 3-5 introdujeron cambios al pipeline informados por el EDA POP
2026-05-09:

- **Fase 3**: drop de duplicados (83 filas), `LOFOutlierScorer` como
  feature additive.
- **Fase 4**: features de tendencia (`t_index_days`, `t_index_years`),
  features skew-mitigated (`KG/HA_LOG1P`, `%INDUS_LOG1P`, `HA_LOG1P`,
  `DPC_SQRT`).
- **Fase 5**: tuning del `n_neighbors` de LOF en search_space.

Cada cambio individual está justificado con findings estadísticos del EDA,
pero la **interacción** entre los cambios y el champion selector
(lex-order: gap → MAPE → tiempo) sólo se puede validar empíricamente.

## Procedimiento de A/B

### 1. Capturar baseline (commit pre-Fase-3)

```bash
# Branch baseline desde el commit anterior a las mejoras EDA
git log --oneline | head -10
# identificar el SHA antes de Fase 3 (drop_duplicates en data_loader)
git checkout -b baseline-pre-eda <SHA>
task build
task train VARIETIES=POP TUNING=dev
```

Capturar:
- Composite score
- MAE_test, MAE_train, gap
- R² mean
- Full MAPE
- Tiempo total
- run_id del champion

### 2. Volver a `main` (con todas las mejoras)

```bash
git checkout main
task build
task train VARIETIES=POP TUNING=dev
```

Capturar las mismas métricas.

### 3. Comparar via `audit:compare`

```bash
task audit:compare -- --variety POP --last 5
```

Tabla esperada:

| Run | Tuning | Backend | composite | MAE_test | gap | R² | tiempo |
|---|---|---|---|---|---|---|---|
| baseline | dev | xgb | ... | ... | ... | ... | ... |
| post-eda | dev | xgb | ... | ... | ... | ... | ... |

### 4. Criterio de aceptación

**Pipeline NEW domina si**:
- composite_score new ≥ baseline (mejor o igual; lex-order)
- gap_new ≤ gap_baseline + 0.005 (no degrada overfitting más allá de
  ruido de ~5%)
- MAPE_new ≤ MAPE_baseline + 1pp (no se degrada > 1 punto porcentual)

**Si NEW no domina**: investigar via residual diagnostics (ya generados
automáticamente por el run new):

- Si `lof_score` aparece en top-5 feature importance → LOF aporta valor
- Si `t_index_*` aparece en top-5 → tendencia está siendo capturada
- Si `<col>_LOG1P/SQRT` están entre las usadas → transformaciones útiles

Si NINGUNO de los nuevos features aparece en top-15: revertir Fases 3-4
parcialmente. Probable culpable: ruido de 4 features extra.

### 5. Estabilidad: re-correr 3x

```bash
# Cambiar RANDOM_STATE y re-correr para medir varianza
# (No esta expuesto via CLI; necesita modificar config.py)
```

Si la varianza cross-seed es > 5% en composite_score, el cambio no es
robusto. Considerar:
- Subir `min_child_samples` lower bound en LGB search_space
- Bajar `learning_rate` upper bound
- Forzar `colsample_bytree` lower a 0.7

### 6. Backtest temporal

Con FECHA disponible, hold-out las últimas 3 cosechas (~3 meses) de POP:

```python
# Pseudo-codigo, integrar en src/diagnostics/ como new module
df_train = df[df['FECHA'] < fecha_corte]
df_test  = df[df['FECHA'] >= fecha_corte]
# Re-fit pipeline new sobre df_train, predict df_test
# Comparar MAPE OOS vs MAPE OOF (CV)
```

Gap OOS-OOF > 20% indica que el CV temporal no es honesto y los lags +
t_index estaban filtrando información.

### 7. Stress tests

Variedades chicas:
```bash
# Asumiendo VENTURA o JUPITER tienen <500 filas
task train VARIETIES=VENTURA TUNING=dev
```

Verificar:
- No crash por LOF con n_neighbors > n_samples (ya manejado en
  `LOFOutlierScorer.fit` con `n_neigh = min(n_neighbors, n_samples - 1)`)
- skew_mitigated_features no hacen `log1p(0)` invalid (ya manejado con
  shift defensivo)

## Decisión

**Pendiente** ejecución con datos reales. Cuando estén las dos métricas
de baseline y post-eda, este ADR se actualiza a Accepted o
Superseded-by-ADR-XXX si los cambios se revierten.

## Verificación

`task audit:compare -- --variety POP --last 10` muestra ambos runs con
sus métricas. La decisión es declarativa basada en la tabla.

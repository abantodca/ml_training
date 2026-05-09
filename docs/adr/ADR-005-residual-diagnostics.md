# ADR-005: Residual diagnostics en cada training run

- **Estado**: Accepted
- **Fecha**: 2026-05-09
- **Tags**: `diagnostics`, `mlflow`, `champion`

## Contexto

El pipeline producía métricas agregadas (R², MAPE, gap) pero no
diagnosticaba los **residuos** del champion. Sin eso no se puede saber
si el modelo capturó todo el patrón disponible o quedó señal sin
explotar:

- ¿Hay **autocorrelación** residual? (DW, Ljung-Box) → faltan lags o
  STL features.
- ¿Hay **heteroscedasticidad** residual? (BP, White) → considerar
  log-target o regresión Gamma.
- ¿Son los residuos **normales**? (Shapiro/AD/JB) → impacta la validez
  de intervalos asintóticos.

Estos diagnósticos son baratos (sub-segundo) y siempre informativos.

## Opciones evaluadas

### Opción A — Diagnóstico manual on-demand
- **Pros**: no agrega tiempo al training.
- **Contras**: nadie lo corre, el insight se pierde.

### Opción B — Diagnóstico automático en cada run
- **Pros**: trazabilidad por run, MLflow lo guarda como artifact.
- **Contras**: ~1s extra por run.

## Decisión

**Opción B**. Al final de `single_run.train_model`, generar HTML de
residual diagnostics con:

- Tests: Durbin-Watson, Ljung-Box, Breusch-Pagan, White, Shapiro,
  Anderson-Darling, Jarque-Bera.
- Plots: residuos vs predicción, |residuos| vs predicción, histograma
  con KDE, Q-Q plot vs normal.
- Verdict automático con regla-based.

Loguear como `residuals/residuals_<variety>_<run_name>.html` artifact.

## Consecuencias

**Positivas**:
- Cada champion tiene su residual report sin esfuerzo manual.
- El dashboard ejecutivo (`Winner_<variety>.html`) linkea
  automáticamente el residual report como sección "diagnósticos
  vinculados".

**Negativas**:
- ~1s extra por run.

**Migración aplicada**:
- `src/diagnostics/residuals.py` (nuevo).
- `src/orchestration/single_run.py`: invoca `write_residual_report`
  después de log del pipeline a MLflow.
- `src/step_05_evaluate/html/sections.py`: nueva
  `build_diagnostic_links_section` que detecta archivos hermanos en
  `reports/` y los linkea.

## Verificación

- Cada run de training produce un HTML residual en `reports/` y como
  artifact MLflow `residuals/`.
- El Winner dashboard tiene cards linkeando esos reportes.

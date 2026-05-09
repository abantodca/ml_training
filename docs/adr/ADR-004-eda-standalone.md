# ADR-004: EDA como módulo standalone (no parte del pipeline de training)

- **Estado**: Accepted
- **Fecha**: 2026-05-09
- **Tags**: `eda`, `diagnostics`, `architecture`

## Contexto

El pipeline de training no incluía análisis exploratorio de datos
(distribuciones, autocorrelación, heteroscedasticidad, drift, VIF, MI).
Las decisiones de cleaning y feature engineering se tomaban "a ojo" o
basadas en auditorías ad-hoc en notebooks.

Necesitábamos:

1. Un análisis estadístico riguroso que se pueda re-ejecutar entre runs.
2. Que sea reproducible (no notebooks).
3. Que produzca un reporte HTML consumible por stakeholders no técnicos.
4. Que NO retrase el training (puede correr en paralelo o on-demand).

## Opciones evaluadas

### Opción A — EDA inline en step_01_load
- **Pros**: cada training carga la data + diagnostica en una pasada.
- **Contras**: training más lento (~30-60s extra), forzando diagnóstico
  costoso aún en runs `smoke`. Mezcla concerns.

### Opción B — Módulo standalone `src/diagnostics/`
- **Pros**: separación de concerns, on-demand, reusable, no afecta
  performance del training.
- **Contras**: dos comandos (`task eda` + `task train`); el operador
  tiene que recordarlo.

### Opción C — Notebook Jupyter
- **Pros**: interactivo.
- **Contras**: no reproducible, no versionable bien, no se loguea como
  artifact de MLflow.

## Decisión

**Opción B**. Nuevo módulo `src/diagnostics/` con submodulos:

- `statistical_tests.py`: wrappers tipados sobre statsmodels/scipy.
- `distributions.py`: análisis univariado (Shapiro/AD/JB, Box-Cox).
- `temporal.py`: ACF/PACF, DW, LB, ADF/KPSS, STL, drift PSI.
- `multivariate.py`: VIF, correlation, mutual information.
- `plots.py`: plotly figures reutilizables.
- `html_renderer.py`: ensambla el HTML.
- `eda.py`: entrypoint `run_eda(variety) → Path`.

Disponible como `task eda VARIETIES=POP`. El HTML se sube como artifact
MLflow si hay un run activo.

## Consecuencias

**Positivas**:
- Diagnóstico riguroso y reproducible.
- Training no se penaliza.
- Reporte HTML profesional consumible por negocio.
- Tests estadísticos versionados en código.

**Negativas**:
- Step manual extra antes del primer training de una variedad nueva.
- Mantenimiento del módulo.

**Migración aplicada**:
- `src/diagnostics/` (nuevo, 7 archivos).
- `Taskfile.yml`: nueva task `eda`.

## Verificación

- `task eda VARIETIES=POP` produce `reports/EDA_POP_<ts>.html` válido.
- HTML contiene secciones: hallazgos top, calidad, distribuciones,
  temporal, multivariado, drift.

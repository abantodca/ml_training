# ADR-001: Backend MLflow — Postgres + S3 (no más sqlite/file://)

- **Estado**: Accepted
- **Fecha**: 2026-05-09
- **Tags**: `mlflow`, `infra`

## Contexto

El proyecto comenzó con backend MLflow `file://mlruns/` y sqlite
(`sqlite:///mlruns/mlflow.db`) como fallback. Esto generaba dos modos
divergentes:

- **Local sin Docker**: sqlite + filesystem local.
- **Docker / producción**: server HTTP con Postgres + S3.

Cada modo se comportaba distinto: `register_model` retornaba `None`
silenciosamente con file://; `artifact_location` se forzaba a path
local del cliente y rompía cuando el server era remoto; el cliente
tenía que ramificar lógica para detectar el modo.

## Opciones evaluadas

### Opción A — Mantener dual-mode (sqlite local + server remoto)
- **Pros**: dev offline.
- **Contras**: dos caminos de código → bugs sutiles, divergencia con
  producción, register_model rompe en file://.

### Opción B — Backend siempre HTTP server
- **Pros**: un solo modo, Registry siempre funcional, código más simple,
  paridad local/prod.
- **Contras**: requiere `task up` para funcionar local; pierde "abre
  archivo y andá".

## Decisión

**Opción B**. Backend MLflow es siempre un server HTTP con Postgres +
S3. En local lo provee `docker compose up`; en producción AWS Fargate.
`MLFLOW_TRACKING_URI` siempre apunta a HTTP (default
`http://localhost:5000`).

## Consecuencias

**Positivas**:
- Un solo camino de código.
- Model Registry funciona end-to-end en local y prod.
- Eliminamos `MLRUNS_DIR`, `MLRUNS_ARTIFACT_LOCATION`,
  `MLFLOW_DEFAULT_DB`, `_use_local_mlruns`.

**Negativas**:
- Dev offline = `task up` (Docker requerido).
- Runs viejos en `mlruns/` no se migran auto.

**Migración aplicada**:
- `src/config.py`: drop `MLRUNS_DIR`, `MLFLOW_DEFAULT_DB`,
  `MLRUNS_ARTIFACT_LOCATION`, `_use_local_mlruns`.
- `src/step_06_track/mlflow_registry.py`: `set_experiment` ya no fuerza
  `artifact_location`.
- `Dockerfile`: drop `mlruns` de `mkdir -p`.

## Verificación

- `register_model()` siempre devuelve `ModelVersion` no-None.
- Pipeline corre end-to-end con `MLFLOW_TRACKING_URI` apuntando a
  `http://mlflow:5000` (compose) y `https://mlflow.<dominio>` (Fargate).

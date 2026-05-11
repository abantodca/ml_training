# Architecture Decision Records (ADRs)

Cada ADR documenta UNA decisión técnica significativa con su contexto,
opciones evaluadas y la decisión tomada. Sigue el formato de
[Michael Nygard](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions.html).

## Ciclo de vida

- `Proposed` → propuesta en revisión.
- `Accepted` → decisión vigente.
- `Deprecated` → reemplazada por una ADR posterior.
- `Superseded by ADR-NNN` → puntero a la nueva.

## Índice

| ID | Título | Estado |
|---|---|---|
| ADR-001 | Backend MLflow: Postgres + S3 (no más sqlite/file://) | Accepted |
| ADR-002 | El sistema elige el modelo (no flag `--model`) | Accepted |
| ADR-003 | Stack local sin LocalStack: S3 real | Accepted |
| ADR-004 | EDA standalone como módulo separado del pipeline | Accepted |
| ADR-005 | Residual diagnostics en cada training run | Accepted |
| ADR-006 | Validación A/B del pipeline post-EDA | Proposed |
| ADR-007 | Mono-repo para trainer + Terraform (no separar a `ml-training-infra/`) | Accepted |
| ADR-008 | CI/CD sin job de tests hasta que exista `tests/` | Accepted |
| ADR-009 | Image signing (cosign) omitido por ahora | Accepted |

## Cómo agregar una ADR nueva

1. Copiar `_template.md` con el siguiente número correlativo.
2. Completar todas las secciones.
3. Setear `Estado: Proposed`. Cambiar a `Accepted` cuando se mergea.
4. Agregar fila al índice arriba.
5. Si reemplaza otra ADR, actualizar la vieja a `Superseded by ADR-NNN`.

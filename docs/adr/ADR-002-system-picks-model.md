# ADR-002: El sistema elige el modelo — no flag `--model`

- **Estado**: Accepted
- **Fecha**: 2026-05-09
- **Tags**: `cli`, `tuning`, `champion`

## Contexto

El CLI exponía `--model {auto, xgb, lgb, "xgb,lgb", all}` que permitía
al operador forzar un backend específico. El default `auto` entrenaba
todos y dejaba que `champion.select_champion` eligiera.

El proyecto es una **competencia entre modelos**: la métrica de
producción (lex-order: gap → MAPE → tiempo) es la fuente de verdad de
qué modelo gana. Permitir `--model xgb` permite saltarse la métrica.

## Opciones evaluadas

### Opción A — Mantener `--model` con default `auto`
- **Pros**: flexibilidad para debug rápido.
- **Contras**: el operador puede degradar el sistema sin saberlo;
  introduce inconsistencias entre runs.

### Opción B — Quitar `--model` del CLI
- **Pros**: contrato claro, una sola fuente de verdad (la métrica),
  imposible degradar el sistema sin tocar código.
- **Contras**: para debug single-backend hay que comentar código en
  `BACKEND_REGISTRY`.

## Decisión

**Opción B**. El CLI no tiene `--model`. `valid_backends()` del
registry decide qué entrenar; el champion selector decide quién gana.

## Consecuencias

**Positivas**:
- Contrato del proyecto explícito.
- Imposible "olvidar" entrenar un backend.
- Si se agrega NGBoost al `BACKEND_REGISTRY`, entra automáticamente.

**Negativas**:
- Tiempo de training fijo (XGB + LGB siempre).

**Migración aplicada**:
- `src/orchestration/cli.py`: drop arg `--model`, función
  `resolve_models`, `MODEL_TYPE_DEFAULT`.
- `main.py`: usa `valid_backends()` directo.
- `src/config.py`: drop `MODEL_TYPE_DEFAULT`.
- `Taskfile.yml`: drop `MODEL` var, drop `--model` del comando train.
- Help inline lo declara explícitamente.

## Verificación

`python main.py --help` no muestra `--model`. Cualquier intento de
pasarlo falla con argparse error.

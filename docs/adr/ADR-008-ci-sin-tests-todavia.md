# ADR-008: CI/CD sin job de tests hasta que exista `tests/`

- **Estado**: Accepted
- **Fecha**: 2026-05-11
- **Tags**: `cicd`, `testing`, `quality-gates`

## Contexto

Al disenar el workflow `ci.yml` (`GUIA_MLOPS_AWS.md §7.1`) habia que
decidir si incluir un job `test` con `pytest --cov` desde el dia 1.
Estado actual del repo:

- **No existe `tests/`** en el repo (verificado con `find tests/` →
  no encontrado; `.dockerignore` lo lista pero la carpeta no existe).
- El pipeline se valida hoy con **smoke runs** (`task train
  VARIETIES=POP TUNING=smoke`, ~1 min) que ejercitan end-to-end.
- Los gates de calidad **del modelo** (no del codigo) ya existen:
  `CHAMPION_MAX_MAPE`, `CHAMPION_MAX_GAP` en `src/config.py` y el
  workflow `promote.yml` (§7.2) bloquea promotion si los supera.

## Opciones evaluadas

### Opcion A — Agregar job `test` con `pytest --cov --cov-fail-under=60`
- **Pros**: gate de calidad estandar. Bloquea regresiones desde el dia 1.
- **Contras**: el job falla en cada PR porque NO HAY tests. Forzaria
  agregar tests de adorno (e.g. `def test_imports(): import src`) que
  no validan nada real pero hacen pasar el job. Eso es deuda
  encubierta: en 3 meses parecera que tenes coverage cuando no lo
  tenes.

### Opcion B — Agregar job `test` pero con `cov-fail-under=0`
- **Pros**: workflow "completo" en estructura.
- **Contras**: misma trampa: el job pasa trivialmente y la cobertura
  real es cero. Peor: oculta el hecho de que no hay tests.

### Opcion C — Omitir el job hasta que exista `tests/`
- **Pros**: el workflow refleja la realidad. Cuando agregues el primer
  test real, agregar el job + sumarlo a required checks de branch
  protection es un cambio explicito y visible.
- **Contras**: hay una ventana donde un bug de codigo puede entrar a
  prod sin ser detectado por CI. Mitigacion: smoke job post-deploy
  (`ci.yml` §7.1 job `smoke`) corre `--tuning smoke` y falla el deploy
  si el pipeline E2E se rompe.

## Decision

**Opcion C**. El workflow `ci.yml` tiene `lint` + `security` + `build`
+ `deploy` + `smoke`, sin job `test`. La guia §7.1 incluye una nota
explicita explicando el plan para cuando exista `tests/`.

## Consecuencias

**Positivas**:
- El workflow refleja la realidad del codigo, no la aspiracional.
- Agregar tests reales sera un trigger visible (PR con `tests/` +
  modificacion del workflow).
- El smoke job E2E provee una red de seguridad minima.

**Negativas / costos**:
- Una regresion logica que no rompa el smoke (ej. error de calculo en
  una metrica de negocio que igual completa el pipeline) puede entrar
  a prod. Riesgo aceptado: el pipeline tiene gates de modelo que
  detectan degradacion (MAPE > umbral → alarma SNS).

**Migracion requerida al activar tests**:
1. Crear `tests/` con primer caso real (ej. `test_data_loader.py`,
   `test_champion_lex_order.py`).
2. Editar `ci.yml` agregando job `test` antes de `build`:
   ```yaml
   test:
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@v4
       - uses: actions/setup-python@v5
         with: { python-version: "3.13", cache: pip }
       - run: pip install -r requirements.txt -r requirements-dev.txt pytest==8.3.3 pytest-cov==5.0.0
       - run: pytest tests/ --cov=src --cov-fail-under=N   # N empezara bajo (e.g. 30)
   ```
3. Agregar `test` a `needs:` del job `build`.
4. Sumar `test` a required status checks en branch protection (§7.6).
5. Subir `cov-fail-under` conforme se agreguen tests.

## Trigger para reconsiderar

- Una regresion logica entra a prod sin que el smoke la detecte.
- Pasamos a 2+ devs (riesgo de overwrites accidentales).
- Compliance requiere coverage minimo demostrable.

## Verificacion

- `ci.yml` corre lint+security en PRs y build+deploy+smoke en push a
  main. Pasa o falla deterministicamente.
- Al primer test real escrito, el ADR pasa a `Superseded by ADR-XXX`.

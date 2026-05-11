# ADR-007: Mono-repo para trainer + Terraform (no separar a `ml-training-infra/`)

- **Estado**: Accepted
- **Fecha**: 2026-05-11
- **Tags**: `infra`, `repo-structure`, `cicd`

## Contexto

`GUIA_MLOPS_AWS.md` describe el Terraform en `envs/prod/` y sugiere como
default un repo separado `ml-training-infra/`. Al implementar el CI/CD
hubo que decidir donde vive efectivamente el codigo de infra:

- Solo existe **1 entorno** (`prod`). No hay dev/staging.
- El equipo es chico (1-2 personas) y el versionado del trainer + infra
  se mueve sincronizado en la practica (cada deploy de codigo suele
  acompanar un bump de imagen tag en `terraform.tfvars`).
- La guia §7.1 ya esta adaptada con `working-directory: infra/envs/prod`
  y no requiere PAT cross-repo.

## Opciones evaluadas

### Opcion A — Mono-repo (trainer + Terraform en mismo repo, en `infra/`)
- **Pros**: un solo `git clone`, un solo PR para cambios atomicos
  (ej. agregar variable de entorno requiere tocar `main.py` + job-def).
  Sin PAT cross-repo. CI/CD mas simple.
- **Contras**: el repo crece (Terraform suma ~30 archivos `.tf`).
  Reviews mezclan codigo Python y HCL. Permisos uniformes (no podes
  dar `read` solo a infra a un dev).

### Opcion B — Repo separado `ml-training-infra/`
- **Pros**: separation of concerns. Permisos granulares
  (devs de modelos sin acceso a Terraform). CI/CD del trainer
  desacoplado del de infra.
- **Contras**: requiere PAT cross-repo en `ci.yml` (job `deploy` hace
  checkout del repo de infra). Versionado divergente: el image-tag en
  el trainer y el `trainer_image_tag` en infra pueden quedar
  desincronizados. Un cambio atomico = 2 PRs en 2 repos.

### Opcion C — Submodulo git de `ml-training-infra` dentro del repo trainer
- **Pros**: union de los dos mundos.
- **Contras**: submodulos son una fuente comprobada de friccion
  (`git submodule update --recursive`, devs que clonan sin
  `--recurse-submodules`, etc.). No vale el costo para 1 equipo de 1-2.

## Decision

**Opcion A (mono-repo)**. El Terraform vive en `infra/envs/prod/` del
mismo repo que el trainer. La guia y los workflows `.github/` ya estan
adaptados a ese path.

## Consecuencias

**Positivas**:
- Cambios atomicos en un solo PR. Reviews completas.
- CI/CD mas simple: `ci.yml` hace `working-directory: infra/envs/prod`
  sin PAT cross-repo. `terraform-plan.yml` corre en cada PR que toca
  `infra/**`.
- Onboarding mas rapido: un `git clone` y ya tenes todo.

**Negativas**:
- Permisos no granulares: cualquier dev con write puede tocar infra.
  Mitigacion: `CODEOWNERS` para `infra/**` (no implementado todavia).
- Tag de release ambiguo: un tag `v1.2.3` cubre trainer + infra. Si en
  el futuro tenemos cadencias distintas, hay que separar.

**Migracion requerida** al implementarlo: crear `infra/envs/prod/` con
los archivos de §3 de la guia. Los workflows ya estan listos.

## Trigger para reconsiderar (cuando aplicar Opcion B)

- Agregamos un segundo entorno (`dev` o `staging`) Y el equipo crece a
  >2 personas con responsabilidades distintas.
- O: un auditor de compliance requiere separation of duties entre
  quienes modifican modelos y quienes modifican infra.
- O: la cadencia de release del trainer (semanal) se desacopla de la
  de infra (trimestral).

Mientras los 3 sigan correlacionados, el costo de migrar a 2 repos
supera al beneficio.

## Verificacion

- `terraform plan` corre desde `infra/envs/prod/` sin extra checkouts.
- `ci.yml` no usa `secrets.INFRA_REPO_TOKEN`.
- Un PR de feature que requiere cambios en trainer + infra entra como
  UN solo PR revisable.

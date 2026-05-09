# ADR-003: Stack local sin LocalStack — usar S3 real

- **Estado**: Accepted
- **Fecha**: 2026-05-09
- **Tags**: `infra`, `s3`, `docker`

## Contexto

El stack local usaba LocalStack (S3 simulado en `:4566`) + un servicio
`s3-init` que creaba buckets en LocalStack. Esto era barato pero
introducía divergencia con producción:

- LocalStack tiene quirks (eventual consistency simulada distinta,
  algunos endpoints difieren).
- Las credenciales eran dummies (`test`/`test`) — el código de auth
  IAM no se ejercitaba en local.
- Bugs que sólo aparecen contra S3 real (permission denied, bucket
  policies, presigned URLs) no se detectaban.

## Opciones evaluadas

### Opción A — Mantener LocalStack
- **Pros**: $0 storage, funciona offline.
- **Contras**: divergencia silenciosa con prod.

### Opción B — Migrar a S3 real
- **Pros**: paridad 1:1 con prod, código de IAM se ejercita.
- **Contras**: $0.50-2/mes en costo, requiere internet, requiere
  `aws configure`.

### Opción C — Híbrido con compose profiles
- **Pros**: opt-in entre los dos.
- **Contras**: complejidad de mantener dos configs.

## Decisión

**Opción B**. LocalStack y `s3-init` se eliminan del compose. El stack
local usa S3 real con buckets separados (`cabanto-ml-mlflow`,
`cabanto-ml-artifacts`).

Las credenciales NO van en `.env`: se leen del `~/.aws/credentials`
del host (creado con `aws configure`) montado como `:ro` adentro de
los containers via `AWS_SHARED_CREDENTIALS_FILE`.

## Consecuencias

**Positivas**:
- Paridad total local/prod.
- Bugs de IAM/policy se detectan en dev.
- `~/.aws` mounted = creds nunca en el repo ni en variables de
  compose interpoladas.

**Negativas**:
- Costo S3 real (~$1/mes).
- Requiere internet.
- Requiere AWS account configurada.

**Migración aplicada**:
- `docker-compose.yml`: drop `localstack`, `s3-init`,
  `localstack-data`. `mlflow` y `trainer` montan `~/.aws:/aws:ro`
  con `AWS_PROFILE` y `AWS_SHARED_CREDENTIALS_FILE`.
- `.env.example`: ya no pide `AWS_ACCESS_KEY_ID`/`SECRET`. Sólo
  `AWS_PROFILE`, `AWS_DEFAULT_REGION`, nombres de bucket.

## Verificación

- `docker compose config` falla con mensaje claro si faltan vars.
- `aws s3 ls s3://${S3_MLFLOW_BUCKET}/` adentro del container del
  trainer responde con las creds del host.

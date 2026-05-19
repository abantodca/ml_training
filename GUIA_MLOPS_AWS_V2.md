# Guía MLOps AWS — Despliegue a producción de `ml_training`

> Manual operativo para llevar el trainer (`src/`, `main.py`) desde una laptop
> hasta AWS Batch con MLflow Tracking + Registry. La guía se lee en dos
> tramos: **Tramo I (Local)**, Capítulos 1-4, donde se construye el entorno
> Docker desde cero a partir del código fuente; y **Tramo II (AWS)**, Partes
> 1-13, donde el mismo binario se promueve a producción. Cada sección
> termina con una verificación; si falla, no se avanza.

---

## Tabla de contenidos

**Tramo I — Entorno local (desde cero)**

- [Capítulo 1 · Visión general](#capítulo-1--visión-general)
- [Capítulo 2 · Decisiones fijas](#capítulo-2--decisiones-fijas)
- [Capítulo 3 · Prerrequisitos del host](#capítulo-3--prerrequisitos-del-host)
- [Capítulo 4 · Entorno local desde cero](#capítulo-4--entorno-local-desde-cero)

**Tramo II — AWS (producción)**

- Parte 1 · Lifecycle (stand-up / tear-down / rebuild / destroy)
- Parte 2 · Bootstrap irreversible (S3 backend + DynamoDB + OIDC)
- Parte 3 · Módulos Terraform
- Parte 4 · Apply incremental + smoke test
- Parte 5 · Patch del trainer (MAPE a CloudWatch)
- Parte 6 · CI/CD con GitHub Actions
- Parte 7 · Promotion gate
- Parte 8 · Runbook operativo extendido
- Parte 9 · Costos detallados
- Parte 10 · Hardening (futuro)
- Parte 11 · Troubleshooting (catálogo)
- Parte 12 · Apéndices (glosario, conceptos, mapa de archivos)
- Parte 13 · Customizaciones puntuales (addendum, opcional)

---

## Cómo leer esta guía

- **Orden de lectura**: local primero, AWS después. No avances al Tramo II
  sin que el smoke local del Capítulo 4 termine en verde.
- **Punto de partida real**: la única asunción es que el repo tiene `src/`,
  `main.py`, `scripts/` y `requirements.txt`. Todo lo demás — Dockerfile,
  compose, Taskfile, `.env` — se construye en el Capítulo 4.
- **Convención de comandos**: todos los bloques `bash` se ejecutan desde la
  raíz del repo. En Windows, exclusivamente desde **WSL Ubuntu** (no Git
  Bash, no PowerShell).
- **Una sola imagen**: el `Dockerfile` que usa `task build` localmente es el
  mismo binario que `task ecr:build IMG=trainer` empuja a ECR en producción.
- **Convención de avisos**:
  - `> **Nota** — …` aclara el porqué de una decisión.
  - `> **Warning** — …` señala riesgos reales (pérdida de datos, costo
    inesperado, operación irreversible).
- **Convención de verificación**: cada bloque importante cierra con un
  comando o tabla que valida el estado. Si falla, parar y resolver antes
  de seguir.

---

# Tramo I — Entorno local

## Capítulo 1 · Visión general

### 1.1 Qué entrenamos

`ml_training` predice **kg/jornal-hora** (`KG/JR_H`) por variedad a partir
de un Excel histórico de cosechas (`data/BD_HISTORICO_ACUMULADO.xlsx`). El
sistema entrena **XGBoost** y **LightGBM** con Optuna, evalúa con
`TimeSeriesSplit`, y elige campeón por variedad según orden lexicográfico
(gap → MAPE → tiempo).

> **Nota** — Pese al nombre del repo (`ml_random_forest`), los backends
> activos son XGBoost + LightGBM. Random Forest fue reemplazado por
> estabilidad numérica del target con `log1p` + cap-p99.

### 1.2 Dos entornos, una sola imagen

| | Local | Producción AWS |
|---|---|---|
| Compute training | Docker compose (laptop) | AWS Batch + EC2 c6i.2xlarge |
| Tracking server | MLflow container, Postgres en volumen | MLflow en ECS Fargate, RDS Postgres |
| Artifacts store | S3 sandbox | S3 productivo + Model Registry |
| Trigger | `task train` (manual) | GitHub Actions / Lambda dispatcher |
| Imagen del trainer | `Dockerfile` raíz | El mismo `Dockerfile`, push a ECR |

### 1.3 Endpoints en producción

```
http://<ALB-DNS>/             MLflow UI (tracking + Model Registry)
http://<ALB-DNS>/reports/     Dashboards HTML por variedad
http://<ALB-DNS>/artifacts/   Artifacts crudos por run
```

### 1.4 Flujo end-to-end

```
Developer
  │ push a main
  ▼
GitHub Actions (deploy.yml)
  │ lint + test + build + push a ECR (via OIDC)
  ▼
ECR ml-training:<sha>
  │ workflow_dispatch training.yml  (o `aws lambda invoke ml-training-dispatcher`)
  ▼
Lambda dispatcher → AWS Batch SubmitJob (Spot queue)
  │ autoscale 0 → 1 EC2 c6i.2xlarge
  ▼
Container del trainer
  │ 1. hydrate S3_DATA_BUCKET/S3_DATA_KEY → data/training/DB-HISTORICA.xlsx
  │ 2. main.py: por variedad entrena XGB + LGB con Optuna
  │ 3. champion.select_champion()
  │ 4. log a MLflow (Postgres backend + S3 artifacts)
  │ 5. sync_to_s3(artifacts/, reports/) a S3_ARTIFACTS_BUCKET
  ▼
MLflow Model Registry: nueva versión en stage "None"
  │ workflow_dispatch promote.yml
  ▼
Quality gate (MAPE < umbral && A/B contra Production actual)
  │ approval humano en GitHub Environments
  ▼
MLflow Model Registry: versión transicionada a "Production"
```

### 1.5 Costo objetivo

| Configuración | Costo mensual aproximado |
|---|---|
| Scheduler L-V 08-12 PET (default) | ~$68 |
| Sin scheduler (24/7) | ~$140 |
| Hibernado (solo storage) | ~$8 |

Detalle en Parte 9 (costos por servicio y por modo de lifecycle).

---

## Capítulo 2 · Decisiones fijas

Las siguientes decisiones no se discuten dentro de esta guía. Cambiar
alguna implica un ADR previo y reescritura de las secciones afectadas.

| Decisión | Elección | Por qué | Cambia a futuro |
|---|---|---|---|
| Región AWS | `us-east-1` | Latencia razonable desde Perú, todos los servicios disponibles, mejor precio Spot. | `us-east-2` o `sa-east-1` por compliance. |
| Compute training | Batch + EC2 c6i.2xlarge, queues Spot (default) + On-Demand (sólo `prod_xl`), `retry=2`. | −70% costo con Spot; retry cubre interrupciones (~5-10% en c6i.2xlarge). | Fargate Spot, o `g5.xlarge` si pasás a DL. |
| Compute serving | ECS Fargate | Sin gestión de host, autoscale, integración nativa con ALB. | EC2 con AMI custom para aceleración. |
| Backend MLflow | Postgres + S3 (artifacts) | Estándar industria; soporta concurrencia. | Filesystem sólo en dev. |
| RDS | Postgres 15, `db.t4g.micro`, single-AZ | $13/mes; suficiente para <10 GB de metadata. | Multi-AZ (Parte 10.4). |
| Auto on/off | Scheduler EventBridge L-V 08-12 PET; chequeo de Batch RUNNING antes de apagar. | UI 4 h/día; training off-window despierta servicios on-demand. | 24/7 si hay equipo distribuido. |
| TLS / WAF / Multi-AZ | NO (ALB :80 HTTP, sin WAF, RDS single-AZ) | Default barato. MLflow con basic auth + SG restrictivo. | Parte 10.1-10.4 antes de Internet abierta. |
| Egress privado | NAT GW single-AZ ($32/mes) | Setup simple si tráfico <10 GB/mes. | VPC endpoints (Parte 10.3). |
| Trigger training | (a) GHA `training.yml` workflow_dispatch (wake-train-sleep); (b) `aws lambda invoke ml-training-dispatcher`. Sin cron, sin S3 PutObject trigger. | Click desde GitHub UI eligiendo variedad. Training off-window wake-ea servicios y los apaga al terminar. | EventBridge cron diario / S3 trigger (Parte 7.5). |
| Modelos entrenables | **XGBoost + LightGBM** sobre `KG/JR_H`, con `TransformedTargetRegressor` (`log1p` + cap-p99). Champion automático. | Lo que vive en `src/step_04_train/registry.py`. | Stacking (eliminado, no existe). |
| Variedades válidas | **Dinámicas**: hojas del Excel `BD_HISTORICO_ACUMULADO.xlsx`. | Source of truth = el Excel. `list_varieties()` enumera `pd.ExcelFile(path).sheet_names`. La variable Terraform `varieties_allowed` es un allow-list defensivo del Lambda dispatcher, no la definición. | Agregar variedad = agregar hoja + `aws s3 cp` + opcional ampliar `varieties_allowed`. |
| Auth CI/CD | OIDC (sin access keys de larga duración) | Auditable en CloudTrail, sin rotación manual, blast-radius limitado al repo. | Keys sólo en CI legacy. |
| Promotion | Quality gate (MAPE < umbral) + A/B contra Production + approval en GitHub Environments | Defense in depth; un MAPE menor no garantiza modelo mejor sin baseline. | Auto-promote si MAPE absoluto <5% (no recomendado). |

---

## Capítulo 3 · Prerrequisitos del host

### 3.1 Herramientas

| Herramienta | Versión mínima | Verificación |
|---|---|---|
| Docker | 24+ con BuildKit | `docker version`, `docker info \| grep "Server Version"` |
| Git | 2.30+ | `git --version` |
| AWS CLI v2 | 2.0+ | `aws --version` |
| Task | 3.34+ | `task --version` |
| Terraform | 1.6+ (sólo Tramo II) | `terraform version` |
| jq | 1.6+ (post-apply checks) | `jq --version` |

Instalación de Task en Linux / WSL Ubuntu:

```bash
sh -c "$(curl --location https://taskfile.dev/install.sh)" -- -d -b ~/bin
export PATH="$HOME/bin:$PATH"   # persistir en ~/.bashrc
task --version
```

macOS:

```bash
brew install go-task
```

### 3.2 Windows: WSL Ubuntu obligatorio

El repo vive típicamente en disco Windows
(`C:\Users\<user>\Documents\Proyectos\ml_random_forest\ml_training`) y se
opera **desde WSL Ubuntu** vía el mount `/mnt/c/...`. Toda la guía asume
esa terminal.

```bash
# Desde PowerShell:
wsl -d Ubuntu

# Dentro de WSL:
cd /mnt/c/Users/<user>/Documents/Proyectos/ml_random_forest/ml_training
pwd
```

Tres ajustes una sola vez:

1. **Docker Desktop** → Settings → Resources → WSL integration → enable
   "Ubuntu". El comando `docker` desde WSL pega contra el mismo daemon
   que Docker Desktop.
2. **CRLF / LF**: en WSL sobre NTFS, git puede marcar todo como
   modificado. Normalizar una vez:
   ```bash
   git config --global core.autocrlf input
   git add --renormalize .
   ```
3. **Permisos POSIX**: NTFS no persiste el bit ejecutable. Invocá scripts
   con `bash infra/<script>.sh`, no `./<script>.sh`.

> **Warning** — En Windows, NO mezclar Git Bash ni PowerShell con WSL.
> Diferencias sutiles de line endings y rutas rompen Terraform, Task y
> Docker. Una sola terminal de principio a fin: WSL Ubuntu.

### 3.3 Credenciales AWS

Aunque el Tramo I es "local", el trainer sube artifacts a S3 y MLflow
escribe sus runs a S3, así que necesitás credenciales válidas desde el
primer `task up`.

```bash
aws configure --profile default
# Access Key ID, Secret Access Key, region us-east-1, output json

aws sts get-caller-identity
# { "UserId": "...", "Account": "<12-digitos>", "Arn": "arn:aws:iam::...:user/..." }
```

> **Warning** — Las credenciales viven **siempre** en `~/.aws/credentials`
> del host. `docker-compose.yml` monta `~/.aws:/aws:ro` en los containers
> y el SDK las lee de ahí. Nunca pongas `AWS_ACCESS_KEY_ID` ni
> `AWS_SECRET_ACCESS_KEY` en `.env` ni en el `Dockerfile`.

### 3.4 Service quotas (sólo para Tramo II)

Los aumentos tardan 24-48 h: pedirlos **antes** del primer `terraform apply`.

| Servicio | Quota | Mínimo |
|---|---|---|
| EC2 Running On-Demand Standard (A/C/D/H/I/M/R/T/Z) | `L-1216C47A` | 32 vCPU |
| EC2 All Standard Spot Instance Requests | `L-34B43A08` | 32 vCPU |
| VPC NAT gateways per AZ | `L-FE5A380F` | 5 (default) |

```bash
aws service-quotas request-service-quota-increase \
  --service-code ec2 \
  --quota-code L-1216C47A \
  --desired-value 32
```

### 3.5 Variables de sesión

Setear una vez por terminal antes de operar Tramo II:

```bash
export AWS_DEFAULT_REGION="us-east-1"
export AWS_PROFILE="default"          # o el profile que uses
export PROJECT="ml-training"
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export ACCOUNT_SUFFIX="${ACCOUNT_ID: -6}"
```

| Variable | Valor | Usada para |
|---|---|---|
| `$PROJECT` | `ml-training` | Prefijo de todos los recursos AWS. |
| `$ACCOUNT_ID` | 12 dígitos | tfstate bucket, ECR URIs, role ARNs. |
| `$ACCOUNT_SUFFIX` | 6 dígitos | Sufijo de buckets, evita colisión cross-account. |
| `$AWS_DEFAULT_REGION` | `us-east-1` | Scope del deployment. |

> **Nota** — Estas variables las usan los bloques `bash` manuales (Tramo II,
> Partes 1-2). Los Taskfiles (`tasks/*.yml`) las recalculan internamente con
> `aws sts get-caller-identity`; no las heredan del shell.

---

## Capítulo 4 · Entorno local desde cero

> **Objetivo del capítulo** — Partiendo de un repo que sólo tiene `src/`,
> `main.py`, `scripts/` y `requirements.txt`, construir todos los artefactos
> de Docker + Compose + Task + `.env` hasta ejecutar un smoke training de
> ~1 minuto contra MLflow y S3 sandbox.

### 4.1 Punto de partida

Verificá que tenés lo mínimo (todo lo demás se construye en este capítulo):

```bash
ls -1 src main.py requirements.txt scripts/prepare_data.py
# src
# main.py
# requirements.txt
# scripts/prepare_data.py
```

Si alguno falta, no continúes: es código que viene del repo.

### 4.2 Layout objetivo

Al cerrar el capítulo, la raíz debe tener esta estructura (en negrita lo
que se construye aquí):

```
ml_training/
├── src/                          (existente)  código del trainer
├── main.py                       (existente)  CLI entrypoint
├── requirements.txt              (existente)  deps Python
├── scripts/prepare_data.py       (existente)  data split
├── Dockerfile                    (§4.4)       imagen del trainer
├── .dockerignore                 (§4.3)       qué NO va al build
├── docker/
│   ├── mlflow/Dockerfile         (§4.5.1)     MLflow + psycopg2 + boto3
│   └── nginx-reports.conf        (§4.5.2)     nginx static
├── docker-compose.yml            (§4.5.3)     postgres + mlflow + reports + trainer
├── Taskfile.yml                  (§4.6)       tasks locales + namespaces AWS
├── tasks/local.yml               (§4.6.2)     helper para buckets sandbox
├── .env.example                  (§4.7)       plantilla de variables
└── .env                          (§4.7)       tu copia con buckets reales
```

### 4.3 `.dockerignore`

El Dockerfile usa `COPY` selectivo (sólo `src/`, `scripts/`, `main.py`,
`requirements.txt`). Sin un `.dockerignore`, el build context que se envía
al daemon arrastra `.git/` (cientos de MB), `data/*.xlsx`, `artifacts/`,
`mlruns/` y caches Python — la build pasa de segundos a minutos y la
imagen crece innecesariamente.

Crear `.dockerignore` en la raíz:

```gitignore
# Git
.git/
.gitignore
.gitattributes

# Datos locales (se montan como volumen en runtime)
data/

# Salidas (se generan en runtime, montadas como volumen)
artifacts/
logs/
reports/

# Legacy del modo file://
mlruns/

# Notebooks y experimentación
notebooks/

# Cache Python
__pycache__/
**/__pycache__/
*.py[cod]
*.pyo

# Tests y caches de tooling
tests/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/

# Entornos virtuales
.venv/
venv/
env/

# IDE
.vscode/
.idea/

# Documentación
*.md
docs/
```

**Verificación**

```bash
# Tras crearlo, una build dry-run sólo debería transferir KBs, no MBs
docker build --progress=plain --no-cache -t ml-training:dryrun . 2>&1 | head -5
# transferring context: ...kB     ← debe ser kB, no MB
```

### 4.4 `Dockerfile`

Imagen multi-stage. El **builder** compila wheels (necesita
`build-essential`) y el **runtime** sólo monta el código + wheels
pre-compilados, manteniendo la imagen final pequeña y sin compiladores.

#### 4.4.1 Stage 1 — builder

```Dockerfile
# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.13.1-slim-bookworm

FROM python:${PYTHON_VERSION} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./

# Cache mount de BuildKit: el pip cache persiste entre builds
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /wheels -r requirements.txt
```

> **Nota** — `# syntax=docker/dockerfile:1.7` habilita el cache mount.
> Sin eso, `RUN --mount=type=cache` se ignora silenciosamente y cada
> build re-descarga las wheels.

#### 4.4.2 Stage 2 — runtime

```Dockerfile
FROM python:${PYTHON_VERSION} AS runtime

ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown
ARG VERSION=dev
LABEL org.opencontainers.image.title="ml-training" \
      org.opencontainers.image.description="Random Forest training pipeline" \
      org.opencontainers.image.source="https://github.com/abantodca/ml_training" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_HOME=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates tini git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 mluser \
    && useradd  --system --uid 1001 --gid mluser --home ${APP_HOME} mluser

WORKDIR ${APP_HOME}

COPY --from=builder /wheels /wheels
COPY requirements.txt ./
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# Orden de COPY: de mejor cache (cambia poco) a peor cache (cambia más)
COPY --chown=mluser:mluser src/    ./src/
COPY --chown=mluser:mluser scripts/ ./scripts/
COPY --chown=mluser:mluser main.py  ./

# Directorios que init_dirs() asume (idempotente)
RUN mkdir -p data/training logs artifacts reports \
    && chown -R mluser:mluser ${APP_HOME}

USER mluser
STOPSIGNAL SIGTERM

# tini propaga SIGTERM correctamente cuando Batch mata el job
ENTRYPOINT ["/usr/bin/tini", "--", "python", "main.py"]
CMD ["--varieties", "POP", "--tuning", "smoke"]
```

> **Nota** — `git` se instala en el runtime porque
> `mlflow.utils.git_utils` y `collect_run_metadata` lo usan para taggear
> el run con `git_commit`. Sin git, todos los runs salen con
> `git_commit=unknown`, rompiendo la trazabilidad modelo → SHA.

> **Nota** — El `USER mluser` (uid 1001) corresponde con los bind-mount
> targets que crea `task _ensure_dirs` (§4.6). Si saltás la task de
> ensure, el primer write del container falla con `Permission denied`.

**Verificación**

```bash
docker build -t ml-training:local .
docker images ml-training:local
# REPOSITORY    TAG     IMAGE ID       SIZE
# ml-training   local   <id>           ~1.2 GB
```

### 4.5 Servicios `docker/` y `docker-compose.yml`

Cuatro servicios en total. `postgres` y `mlflow` son fondo, `reports` sirve
archivos estáticos, `trainer` es one-shot (lo invoca `task train`).

| Servicio | Imagen | Rol | Puerto host |
|---|---|---|---|
| `postgres` | `postgres:15-alpine` | Backend store de MLflow (metadata, Registry) | — (interno) |
| `mlflow` | Build de `docker/mlflow/Dockerfile` | Tracking server v3.12 + UI | `127.0.0.1:5000` |
| `reports` | `nginx:1.27-alpine` | Sirve `./reports/` y `./artifacts/` del host | `127.0.0.1:8080` |
| `trainer` | Build del `Dockerfile` raíz | One-shot `main.py` con args | — |

#### 4.5.1 `docker/mlflow/Dockerfile`

MLflow upstream **no incluye** `psycopg2`, sin el cual
`--backend-store-uri postgresql://...` falla al arranque. Tampoco trae
`boto3`, que se necesita para escribir artifacts en S3. Imagen custom
mínima:

```Dockerfile
FROM ghcr.io/mlflow/mlflow:v3.12.0

RUN pip install --no-cache-dir \
        psycopg2-binary==2.9.9 \
        boto3==1.38.0

LABEL org.opencontainers.image.title="mlflow-with-pg-s3" \
      org.opencontainers.image.description="MLflow 3.12.0 + psycopg2-binary + boto3" \
      org.opencontainers.image.source="https://github.com/abantodca/ml_training" \
      org.opencontainers.image.base.name="ghcr.io/mlflow/mlflow:v3.12.0"
```

#### 4.5.2 `docker/nginx-reports.conf`

Sirve `reports/` y `artifacts/` del host como HTTP estático. Autoindex on
hace navegables los directorios; un `Content-Disposition: attachment`
fuerza descarga de los `.joblib` / `.xlsx` (no son útiles inline).

```nginx
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;

    autoindex on;
    autoindex_exact_size off;
    autoindex_localtime on;

    location / {
        try_files $uri $uri/ =404;
    }

    location ~ \.(joblib|xlsx|json)$ {
        add_header Content-Disposition 'attachment';
    }
}
```

#### 4.5.3 `docker-compose.yml`

Las decisiones sutiles del compose, explicadas inline en los comentarios
del archivo, son:

- **Logging con rotación**: sin `max-size`, el `json-file` driver crece sin
  bound. Postgres + MLflow son los peores ofensores en runs largos.
- **Healthchecks**: `postgres` valida `-d mlflow` (no sólo `pg_isready`);
  `mlflow` polea `/health` (necesario porque MLflow 3.x activa middleware
  anti-DNS-rebinding y rechaza requests antes de estar listo).
- **`--allowed-hosts`**: MLflow 3.x sólo acepta el `Host` header que
  coincide con la lista. El cliente del trainer pega contra `mlflow:5000`,
  así que hay que incluir `mlflow:*` además del `localhost:*` default.
- **Credenciales AWS por bind-mount**: `~/.aws:/aws:ro` montado en los
  containers que las necesitan; nunca duplicadas en `.env`.
- **Loopback bind** (`127.0.0.1:5000:5000`): no exponer MLflow a la red
  local de la laptop (relevante en VMs corporativas o WSL con bridge).

```yaml
# Logging con rotación. Sin esto json-file crece sin bound.
x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"

services:
  postgres:
    image: postgres:15-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: mlflow
      POSTGRES_USER: mlflow
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-mlflow}
    volumes:
      - pg-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U mlflow -d mlflow"]
      interval: 5s
      retries: 10
    logging: *default-logging

  mlflow:
    build:
      context: .
      dockerfile: docker/mlflow/Dockerfile
    restart: unless-stopped
    depends_on:
      postgres: { condition: service_healthy }
    environment:
      AWS_SHARED_CREDENTIALS_FILE: /aws/credentials
      AWS_CONFIG_FILE: /aws/config
      AWS_PROFILE: ${AWS_PROFILE:-default}
      AWS_DEFAULT_REGION: ${AWS_DEFAULT_REGION:-us-east-1}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-mlflow}
    volumes:
      - ~/.aws:/aws:ro
    command: >
      sh -c "mlflow server
      --host 0.0.0.0 --port 5000
      --allowed-hosts mlflow,mlflow:*,localhost,localhost:*,127.0.0.1,127.0.0.1:*
      --backend-store-uri postgresql://mlflow:$${POSTGRES_PASSWORD}@postgres:5432/mlflow
      --default-artifact-root s3://${S3_MLFLOW_BUCKET:?Set S3_MLFLOW_BUCKET in .env}/artifacts"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5000/health',timeout=3).status==200 else 1)"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 30s
    ports:
      - "127.0.0.1:5000:5000"
    logging: *default-logging

  reports:
    image: nginx:1.27-alpine
    restart: unless-stopped
    volumes:
      - ./reports:/usr/share/nginx/html/reports:ro
      - ./artifacts:/usr/share/nginx/html/artifacts:ro
      - ./docker/nginx-reports.conf:/etc/nginx/conf.d/default.conf:ro
    ports:
      - "127.0.0.1:8080:80"
    logging: *default-logging

  trainer:
    build: .
    depends_on:
      mlflow: { condition: service_healthy }
    environment:
      MLFLOW_TRACKING_URI: ${MLFLOW_TRACKING_URI:-http://mlflow:5000}
      AWS_SHARED_CREDENTIALS_FILE: /aws/credentials
      AWS_CONFIG_FILE: /aws/config
      AWS_PROFILE: ${AWS_PROFILE:-default}
      AWS_DEFAULT_REGION: ${AWS_DEFAULT_REGION:-us-east-1}
      S3_ARTIFACTS_BUCKET: ${S3_ARTIFACTS_BUCKET:?Set S3_ARTIFACTS_BUCKET in .env}
      S3_ARTIFACTS_PREFIX: artifacts
      S3_REPORTS_PREFIX: reports
    volumes:
      - ~/.aws:/aws:ro
      - ./data:/app/data
      - ./logs:/app/logs
      - ./artifacts:/app/artifacts
      - ./reports:/app/reports
    mem_limit: ${TRAINER_MEM:-8g}
    cpus: ${TRAINER_CPUS:-4}
    command: ["--varieties", "${VARIETIES:-POP}", "--tuning", "${TUNING:-smoke}"]
    logging: *default-logging

volumes:
  pg-data:
```

> **Nota** — `MLFLOW_TRACKING_URI` está parametrizado por shell var. Esto
> deja la puerta abierta a apuntar el trainer local contra el MLflow
> productivo (`export MLFLOW_TRACKING_URI=http://<ALB-DNS>`) sin tocar el
> compose. Detalle en Parte 13.8.

### 4.6 `Taskfile.yml`

Task orquesta tres cosas: (a) build/up/down de Docker, (b) ejecutar el
trainer con argumentos parametrizables, (c) operaciones AWS namespaced
(`infra:`, `ecr:`, `batch:`, …). Los namespaces AWS se importan desde
`tasks/*.yml` que se construyen en la Parte 4 — por ahora declaramos los
`includes:` para que el Taskfile esté listo, y dejamos que `task --list`
funcione aunque las tasks AWS aún no existan.

#### 4.6.1 Raíz `Taskfile.yml`

```yaml
version: "3"

# `.env` es opcional. Si no existe, los defaults de config.py aplican.
dotenv: [ ".env" ]

# Cada include prefija con su namespace, así las tasks locales no
# chocan con las AWS (infra:apply, ecr:build, ...).
includes:
  infra:      { taskfile: ./tasks/infra.yml,           vars: { PROJECT: '{{.PROJECT}}', REGION: '{{.REGION}}' } }
  ecr:        { taskfile: ./tasks/ecr.yml,             vars: { PROJECT: '{{.PROJECT}}', REGION: '{{.REGION}}' } }
  batch:      { taskfile: ./tasks/batch.yml,           vars: { PROJECT: '{{.PROJECT}}', REGION: '{{.REGION}}' } }
  cluster:    { taskfile: ./tasks/cluster.yml,         vars: { PROJECT: '{{.PROJECT}}', REGION: '{{.REGION}}' } }
  mlflow-aws: { taskfile: ./tasks/mlflow_registry.yml, vars: { PROJECT: '{{.PROJECT}}', REGION: '{{.REGION}}' } }
  aws:        { taskfile: ./tasks/aws.yml,             vars: { PROJECT: '{{.PROJECT}}', REGION: '{{.REGION}}' } }
  local:      { taskfile: ./tasks/local.yml,           vars: { PROJECT: '{{.PROJECT}}', REGION: '{{.REGION}}' } }

vars:
  TUNING:    '{{.TUNING    | default "prod"}}'
  VARIETIES: '{{.VARIETIES | default "POP"}}'
  PARALLEL:  '{{.PARALLEL  | default "1"}}'
  PROJECT:   '{{.PROJECT   | default "ml-training"}}'
  REGION:    '{{.AWS_DEFAULT_REGION | default "us-east-1"}}'

tasks:

  lint:
    desc: "Lint Python (ruff). Corre en el host"
    cmds:
      - ruff check src/ main.py scripts/

  test:
    desc: "Tests con cobertura (pytest). Si no hay tests/, no falla"
    cmds:
      - |
        if [ -d tests ]; then
          pytest tests/ --cov=src --cov-report=term-missing
        else
          echo "No tests/ dir, skip"
        fi

  data:split:
    desc: "Genera data/training/DB-HISTORICA.xlsx desde el Excel histórico"
    cmds:
      - docker compose run --rm --no-deps --entrypoint python trainer
        -m scripts.prepare_data
        --input  data/BD_HISTORICO_ACUMULADO.xlsx
        --output data/training/DB-HISTORICA.xlsx
        --min-rows 100

  eda:
    desc: "EDA estadístico standalone. Args: VARIETIES=POP"
    cmds:
      - docker compose run --rm --no-deps --entrypoint python trainer
        -m src.diagnostics.eda
        --variety {{.VARIETIES}}

  build:
    desc: "Rebuild de la imagen del trainer + levanta servicios"
    cmds:
      - task: _ensure_dirs
      - docker compose build trainer
      - docker compose up -d postgres mlflow reports
      - task: _print_urls

  up:
    desc: "Levanta servicios sin rebuild (postgres + mlflow + reports)"
    cmds:
      - task: _ensure_dirs
      - docker compose up -d postgres mlflow reports
      - task: _print_urls

  down:
    desc: "Detiene servicios. Preserva volumen Postgres"
    cmds:
      - docker compose down

  clean:docker:
    desc: "DESTRUCTIVO: detiene servicios y borra volumen Postgres"
    prompt: "Esto borra TODO el historial MLflow (metadata). Artifacts en S3 no se tocan. Continuar?"
    cmds:
      - docker compose down -v

  logs:
    desc: "tail en vivo de logs Docker (trainer + mlflow)"
    cmds:
      - docker compose logs -f --tail=200 trainer mlflow

  train:
    desc: "Entrena dentro del container. Vars: VARIETIES TUNING PARALLEL"
    deps: [up]
    vars:
      GIT_SHA:
        sh: git rev-parse HEAD 2>/dev/null || echo unknown
      GIT_DIRTY:
        sh: git diff --quiet HEAD 2>/dev/null && echo false || echo true
    cmds:
      - docker compose run --rm
        -e GIT_SHA={{.GIT_SHA}}
        -e GIT_DIRTY={{.GIT_DIRTY}}
        trainer
        --varieties {{.VARIETIES}}
        --tuning {{.TUNING}}
        --parallel-varieties {{.PARALLEL}}

  reports:dashboard:
    desc: "Regenera reports/index_static.html (snapshot estático)"
    cmds:
      - docker compose run --rm --no-deps --entrypoint python trainer
        -m src.diagnostics.dashboard_index

  _ensure_dirs:
    internal: true
    silent: true
    cmds:
      - mkdir -p artifacts reports logs data/training

  _print_urls:
    internal: true
    silent: true
    cmds:
      - 'echo ""'
      - 'echo "==============================================================="'
      - 'echo " Servicios listos:"'
      - 'echo "   MLflow UI       http://localhost:5000"'
      - 'echo "   Reports / HTML  http://localhost:8080/reports/"'
      - 'echo "   Artifacts       http://localhost:8080/artifacts/"'
      - 'echo "   S3 backend      s3://${S3_MLFLOW_BUCKET}/"'
      - 'echo "==============================================================="'
      - 'echo ""'
```

> **Nota** — `prompt:` requiere Task 3.34+. En versiones más viejas se
> ignora silenciosamente y `clean:docker` corre sin confirmación.

| Variable | Default | Override por CLI |
|---|---|---|
| `VARIETIES` | `POP` | `task train VARIETIES=POP,VENTURA` |
| `TUNING` | `prod` | `task train TUNING=smoke` (`smoke` / `dev` / `prod` / `prod_xl`) |
| `PARALLEL` | `1` | `task train VARIETIES=all PARALLEL=3` |

Perfiles de `TUNING`:

| Perfil | Tiempo aprox. | CV | Uso |
|---|---|---|---|
| `smoke` | ~1 min | 2×2 | Sanity check |
| `dev` | ~20 min | 3×3 | Baseline rápido |
| `prod` | ~2 h | 5×3 | Producción (default) |
| `prod_xl` | ~6 h | 6×3 | Búsqueda exhaustiva (overnight) |

#### 4.6.2 `tasks/local.yml`

Helper para crear los buckets S3 sandbox de forma idempotente. Se invoca
en §4.8.

```yaml
version: "3"

vars:
  SUFFIX:
    sh: aws sts get-caller-identity --query Account --output text | tail -c 7

tasks:

  ensure-buckets:
    desc: "Crea S3 buckets data + artifacts si no existen (idempotente)"
    silent: true
    cmds:
      - task: _ensure-bucket
        vars: { NAME: '{{.PROJECT}}-data-{{.SUFFIX}}' }
      - task: _ensure-bucket
        vars: { NAME: '{{.PROJECT}}-artifacts-{{.SUFFIX}}' }
      - 'echo ""'
      - 'echo "Listo. Para que el trainer local sincronice, exporta:"'
      - 'echo "  export S3_DATA_BUCKET={{.PROJECT}}-data-{{.SUFFIX}}"'
      - 'echo "  export S3_ARTIFACTS_BUCKET={{.PROJECT}}-artifacts-{{.SUFFIX}}"'

  bucket-name:
    desc: "Imprime el nombre del bucket. Var: KIND=data|artifacts"
    silent: true
    requires:
      vars: [KIND]
    cmds:
      - echo "{{.PROJECT}}-{{.KIND}}-{{.SUFFIX}}"

  _ensure-bucket:
    internal: true
    silent: true
    requires:
      vars: [NAME]
    cmds:
      - |
        if aws s3api head-bucket --bucket "{{.NAME}}" 2>/dev/null; then
          echo "  {{.NAME}}  EXISTE (reuso)"
          exit 0
        fi
        echo "  {{.NAME}}  no existe -> creando..."
        if [ "{{.REGION}}" = "us-east-1" ]; then
          aws s3api create-bucket --bucket "{{.NAME}}" --region {{.REGION}}
        else
          aws s3api create-bucket --bucket "{{.NAME}}" --region {{.REGION}} \
            --create-bucket-configuration LocationConstraint={{.REGION}}
        fi
        aws s3api put-bucket-versioning --bucket "{{.NAME}}" \
          --versioning-configuration Status=Enabled
        aws s3api put-bucket-encryption --bucket "{{.NAME}}" \
          --server-side-encryption-configuration \
          '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
        aws s3api put-public-access-block --bucket "{{.NAME}}" \
          --public-access-block-configuration \
          'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'
        echo "  {{.NAME}}  CREADO (versioning + AES256 + no public)"
```

> **Nota** — `us-east-1` rechaza `--create-bucket-configuration` (es la
> región default de S3 y el flag tira `InvalidLocationConstraint`). Por
> eso la rama del `if [ "{{.REGION}}" = "us-east-1" ]`.

**Verificación**

```bash
task --list
# Debe mostrar: build, up, down, train, data:split, eda, lint, test,
# reports:dashboard, local:ensure-buckets, local:bucket-name
```

Si `task --list` da errores tipo `failed to read taskfile`, revisar
indentación YAML — Task es estricto con tabs vs spaces.

### 4.7 `.env.example` y `.env`

`docker-compose.yml` exige dos variables obligatorias
(`S3_MLFLOW_BUCKET`, `S3_ARTIFACTS_BUCKET`); las demás tienen defaults
sanos.

#### 4.7.1 Crear `.env.example`

```bash
# AWS (sin secrets)
# Las credenciales VIVEN EN ~/.aws/credentials del host (creado con
# `aws configure`). docker-compose monta ~/.aws:ro en los containers
# y el SDK las lee via AWS_PROFILE + AWS_SHARED_CREDENTIALS_FILE.
AWS_PROFILE=default
AWS_DEFAULT_REGION=us-east-1

# Postgres (MLflow backend store)
# Default sano para localhost-only. Override en cualquier entorno
# compartido o VM en red.
# POSTGRES_PASSWORD=mlflow

# Trainer resource limits (opcional)
# TRAINER_MEM=8g
# TRAINER_CPUS=4

# Buckets S3 (REQUERIDOS)
# Crear con `task local:ensure-buckets` (§4.8)
# Pueden ser el mismo bucket si no necesitás políticas IAM separadas.
S3_MLFLOW_BUCKET=ml-training-artifacts-XXXXXX
S3_ARTIFACTS_BUCKET=ml-training-artifacts-XXXXXX

# MLflow (opcional)
# MLFLOW_TRACKING_URI=http://localhost:5000
# MLFLOW_EXPERIMENT_PREFIX=
# MODEL_REGISTRY_PREFIX=rnd-forest-

# Reporte gerencial (opcional)
# REPORT_PLOTLY_OFFLINE=1
```

#### 4.7.2 Copia para uso real

```bash
cp .env.example .env
# Editar .env: completar los dos buckets con valores reales
```

| Variable | Cuándo override | Default |
|---|---|---|
| `S3_MLFLOW_BUCKET` | **Obligatoria** | (sin default, falla si vacía) |
| `S3_ARTIFACTS_BUCKET` | **Obligatoria** | (sin default, falla si vacía) |
| `AWS_PROFILE` | Profile distinto de `default` | `default` |
| `AWS_DEFAULT_REGION` | Buckets en otra región | `us-east-1` |
| `POSTGRES_PASSWORD` | Equipo / red compartida | `mlflow` |
| `TRAINER_MEM` / `TRAINER_CPUS` | Laptop con <16 GB / <4 CPUs | `8g` / `4` |

### 4.8 Buckets S3 sandbox

Única dependencia AWS para correr local. `task local:ensure-buckets` los
crea idempotentemente con el mismo hardening que el módulo `storage` de
producción (versioning + AES256 + no public access).

```bash
export PROJECT="ml-training"
export AWS_DEFAULT_REGION="us-east-1"

task local:ensure-buckets
#   ml-training-data-<suffix>       CREADO (o EXISTE)
#   ml-training-artifacts-<suffix>  CREADO (o EXISTE)
```

Completar `.env` con los nombres reales:

```bash
SUFFIX=$(aws sts get-caller-identity --query Account --output text | tail -c 7)
sed -i "s|S3_MLFLOW_BUCKET=.*|S3_MLFLOW_BUCKET=ml-training-artifacts-${SUFFIX}|"     .env
sed -i "s|S3_ARTIFACTS_BUCKET=.*|S3_ARTIFACTS_BUCKET=ml-training-artifacts-${SUFFIX}|" .env
```

**Verificación**

```bash
SUFFIX=$(aws sts get-caller-identity --query Account --output text | tail -c 7)
aws s3api get-bucket-versioning --bucket "ml-training-artifacts-${SUFFIX}"
# { "Status": "Enabled" }
```

> **Nota** — El bucket de MLflow y el de `s3_sync` del trainer pueden ser
> el mismo. Sólo separarlos si vas a aplicar políticas IAM o lifecycle
> distintas (Tramo II).

### 4.9 Primera ejecución

Tres comandos en orden, no saltearse:

```bash
# 1) Build de la imagen + arranque de servicios
task build
# Tarda 5-10 min la primera vez (compila wheels en stage 1).
# Al final imprime las URLs:
#   MLflow UI       http://localhost:5000
#   Reports HTML    http://localhost:8080/reports/
#   Artifacts       http://localhost:8080/artifacts/

# 2) Generar el dataset de training
task data:split
# Lee  data/BD_HISTORICO_ACUMULADO.xlsx
# Escribe data/training/DB-HISTORICA.xlsx (1 hoja por variedad)

# 3) Smoke test (~1 min)
task train VARIETIES=POP TUNING=smoke
# Al final del log:
#   FIN | variedades=1 | falladas=0 | tiempo_total=...s
#   Campeones por variedad:
#     POP                       -> xgb composite=...
```

### 4.10 Verificación post-smoke (5 checks)

```bash
# 1) MLflow server responde
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5000/health
# 200

# 2) Postgres tiene el experimento POP con 2 runs (xgb + lgb)
docker compose exec postgres psql -U mlflow -d mlflow -c \
  "SELECT name, (SELECT COUNT(*) FROM runs WHERE experiment_id = e.experiment_id) AS n_runs
   FROM experiments e WHERE name = 'POP';"
# POP | 2

# 3) joblib del campeón en S3
SUFFIX=$(aws sts get-caller-identity --query Account --output text | tail -c 7)
aws s3 ls "s3://ml-training-artifacts-${SUFFIX}/artifacts/" --recursive \
  | grep -E "final_pipeline_POP_.*\.joblib$"
# Al menos un match con timestamp reciente

# 4) nginx sirve los reports
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/reports/
# 200 (o 301 si autoindex redirige)

# 5) run_summary del agregado existe en host
cat artifacts/run_summary_AGGREGATE.json | jq '.champions'
# { "POP": "xgb" }   (o "lgb", depende del run)
```

Si los 5 dan OK, el setup local está validado. Avanzá al Tramo II
cuando tengas tiempo dedicado (el stand-up tarda 2-3 h).

### 4.11 Workflow día a día

Una vez que `task build` corrió al menos una vez, el ciclo iterativo es:

```bash
# Mañana: arrancar servicios (sin rebuild)
task up

# Iterar
task train VARIETIES=POP TUNING=dev          # ~20 min, baseline
task train VARIETIES=POP TUNING=prod         # ~2 h, producción
task train VARIETIES=all PARALLEL=3          # todas en paralelo

# Seguir progreso en vivo
task logs

# Regenerar dashboard agregado sin re-entrenar
task reports:dashboard

# Tras tocar src/ o requirements.txt: rebuild
task build

# Noche: apagar (preserva volumen Postgres + S3)
task down
```

### 4.12 Troubleshooting local

| Síntoma | Causa probable | Fix |
|---|---|---|
| `ERROR: Set S3_MLFLOW_BUCKET in .env` | `.env` ausente o variable vacía | `cp .env.example .env` + completar §4.7 |
| `Unable to locate credentials` en logs MLflow / trainer | `~/.aws/credentials` no existe, o `AWS_PROFILE` apunta a un profile inexistente | `aws configure` + `cat ~/.aws/credentials` |
| `NoSuchBucket` al arrancar mlflow | Buckets en `.env` no existen | `task local:ensure-buckets` |
| `Host header ... not allowed` | Cliente pega contra un host fuera del `--allowed-hosts` | Usar `mlflow:5000` o `localhost:5000`; o editar el `command:` del compose |
| Trainer muere con `OOMKilled` (exit 137) | `mem_limit: 8g` insuficiente | `TRAINER_MEM=16g` en `.env`, después `task down && task up` |
| `Port 5000/8080 already allocated` | Otro proceso usa esos puertos | `lsof -i :5000` (o `:8080`), matar o cambiar `ports:` |
| `task train` no encuentra `DB-HISTORICA.xlsx` | Saltaste `task data:split` | Correr `task data:split` primero |
| `git_commit=unknown` en MLflow tag | El container no monta `.git/` (excluido por `.dockerignore`) | OK en dev local; `task train` ya inyecta `GIT_SHA` via `-e` |
| Postgres healthcheck en `starting` para siempre | Imagen corrupta o disco lleno | `task clean:docker`, `docker system prune`, reintentar |
| `Connection refused: mlflow:5000` desde trainer | Red Docker rota | `task down && task up`; si persiste, `docker network prune` |
| `nginx 403 Forbidden` en `/reports/` | `reports/` vacío o sin permisos | Correr al menos un `task train`; revisar `ls -la reports/` |
| `task train TUNING=prod` se cuelga | `PARALLEL` alto + `TRAINER_CPUS` bajo → oversubscription | Bajar `PARALLEL` o subir `TRAINER_CPUS` |

### 4.13 Próximo paso: Tramo II

Con el smoke local en verde, el código del trainer está validado. El
**mismo** binario se promueve a AWS Batch:

1. Bootstrap del backend Terraform (Parte 2) — UNA vez por cuenta.
2. Aplicar módulos Terraform (Partes 3-4) — VPC, S3, ECR, MLflow ECS, Batch.
3. Build + push de la imagen del trainer a ECR — `task ecr:build IMG=trainer`.
4. Smoke test en Batch — `task batch:smoke`.

Lo que **no cambia** entre local y AWS:

- El `Dockerfile`. Es el mismo binario.
- El código (`main.py`, `src/`). Lee variables de entorno: en local del
  `.env`, en Batch de la job definition.
- El modelo final. Mismo joblib, mismo MAPE.

Lo que **sí cambia**:

| Componente | Local | AWS |
|---|---|---|
| `MLFLOW_TRACKING_URI` | `http://mlflow:5000` | `http://<ALB-DNS>` |
| Postgres | Container en volumen | RDS managed |
| Credenciales AWS | `~/.aws:/aws:ro` montado | IAM Task Role (metadata endpoint) |
| Trigger | `task train` manual | Lambda dispatcher / GHA workflow_dispatch |

> **Nota** — Si querés que tu laptop entrene contra el MLflow productivo
> (sin runs locales huérfanos en el Postgres local), ver Parte 13.8.

---

# Parte 1 — Overview del lifecycle y stand-up

La V1 mezcla los modos a lo largo del runbook. En la V2 son explicitos.
Cada uno responde a una pregunta concreta:

| Modo | Pregunta que responde | Tiempo | Costo despues |
|---|---|---|---|
| **STAND-UP** | "Es la primera vez, parto de cero" | 2-3 horas | ~$68/mes (operando) |
| **TEAR-DOWN** | "No voy a usar la infra por 1+ semana, quiero ahorrar" | 15 min | ~$8/mes (solo storage) |
| **REBUILD** | "Volvi y quiero levantar otra vez sin perder modelos/data" | 20-30 min | ~$68/mes |
| **DESTROY** | "Termine el proyecto / migro a otra cuenta, borra TODO" | 30-45 min | $0/mes |

Diagrama de transiciones:

```
                          stand-up
            (vacio) ─────────────────► OPERATING (64$/mes)
                                          │  ▲
                                          │  │
                                  tear-down  rebuild
                                          │  │
                                          ▼  │
                                       HIBERNATED (8$/mes)
                                          │
                                       destroy
                                          │
                                          ▼
                                       (vacio)
```

> **Que cubre esta Parte 1**: solo el **STAND-UP** (§1.1, abajo) — el unico
> modo que necesitas en una primera lectura, porque todavia no tenes nada
> construido. Los otros 3 modos (TEAR-DOWN / REBUILD / DESTROY) son
> operaciones del runbook y viven en **§8.5-§8.7**: aplican cuando ya
> estuviste operando el sistema.
>
> **Regla de oro**: solo se DESTRUYE cuando estas seguro. Tear-down +
> rebuild es seguro y reversible; destroy NO lo es (perdes state, models
> en Registry, RDS snapshots si no los exportaste).

## 1.1 STAND-UP — primera vez, de cero a produccion

Cuando lo uso: la primera vez que despliego, o tras un **DESTROY**.

### Camino completo

```
Capítulo 3 (prereqs validados)
       │
       ▼
Parte 2 (bootstrap: S3 backend + DynamoDB + OIDC) — 15 min, IRREVERSIBLE
       │
       ▼
Parte 3 (escribir modulos Terraform) — 30-60 min (copy-paste)
       │
       ▼
Parte 4.1 (apply storage solo: ECR + buckets) — 5 min
       │
       ▼
Parte 4.2 (build + push 3 imagenes a ECR) — 20-30 min (primera vez)
       │
       ▼
Parte 4.3 (apply full: network + RDS + Fargate + Batch + Lambdas + ...) — 15-25 min
       │
       ▼
Parte 4.4 (smoke test: 1 job de Batch end-to-end) — 15-20 min
       │
       ▼
Parte 5 (patch trainer + re-push) — 10 min
       │
       ▼
Parte 6 (CI/CD GitHub Actions) — 30 min
       │
       ▼
Parte 7 (promotion gate) — 20 min
       │
       ▼
OPERATING (~$68/mes)
```

**Tiempo total realista**: 2-3 horas la primera vez, asumiendo que los
prereqs (0.3) estan OK y la imagen Docker del trainer ya esta probada
local (0.3.5).

### Lo que NO se hace en stand-up

- Hardening (TLS, WAF, Multi-AZ, KMS-CMK, VPC endpoints, DR cross-region):
  Parte 10, dia 90+.
- Workflows extras (cleanup, drift detection): Parte 6.5, futuro.
- Promotion gate: la primera vez podes saltarte la Parte 7; los primeros
  models entran a `Staging` y los promotes a mano via `mlflow ui`.

## 1.2 Otros modos (TEAR-DOWN / REBUILD / DESTROY)

Estos modos son operaciones del runbook (ya tenes el sistema construido),
no del stand-up inicial. En tu primera lectura no los necesitas — saltalos
y volve cuando ya estes operando. Estan documentados en Parte 8:

- **§8.5 — TEAR-DOWN**: apagar todo preservando state + datos (~$8/mes
  hibernado, reversible con rebuild).
- **§8.6 — REBUILD**: volver despues de un tear-down (cambia solo el ALB
  DNS).
- **§8.7 — DESTROY**: eliminar TODO de la cuenta AWS (requiere 3 backups
  manuales previos — solo aplica si ya operaste el sistema y tenes
  modelos en el Registry, datos en RDS y Terraform state poblado).

La matriz cruzada de costos entre modos (stand-up vs tear-down vs destroy)
esta en §9.3.

---

# Parte 2 — Bootstrap irreversible

## 2.1 Por que el bootstrap es a mano

Terraform necesita un backend remoto (S3 + DynamoDB lock) para que el
state este compartido y safe contra concurrent applies. Pero el backend
no se puede crear con el mismo Terraform que lo usa (chicken-and-egg).

Soluciones posibles:

- **Bootstrap a mano** (lo que hace esta guia): script bash que llama
  AWS CLI directo para crear S3 + DynamoDB. Una vez. **No versionado
  en Terraform**. Si lo destruis, lo recreas a mano.
- Terraform con backend local + `terraform state push` despues: mas
  complejo, mas error-prone.
- CloudFormation seed stack: agrega otra herramienta a la pila.

Elegimos (1) porque es 50 lineas de bash, ejecutables UNA vez,
auditable a simple vista, y el "perdes el state" se mitiga con
versioning del bucket S3 (paso 2.4 lo valida).

Lo mismo aplica al **OIDC provider** de GitHub: si lo creas con
Terraform y haces destroy, el proximo GH Actions falla. Por eso se
bootstrap-ea aparte en 2.5.

## 2.2 Script de bootstrap (bash)

Crear el archivo `infra/bootstrap.sh` con el contenido completo de
abajo. Si ya existe (este repo lo tiene), comparar con `diff` antes de
sobreescribir.

> **Convencion de copy-paste**: cada bloque de codigo en esta guia
> esta precedido por un encabezado con el path destino (`infra/X.sh`,
> `infra/modules/Y/main.tf`, etc.). Crear el archivo en ese path con
> editor o `cat > path <<'EOF' ... EOF` y pegar el contenido del
> bloque. NO mezclar bloques de archivos distintos.

> **Equivalente en AWS Console** — esto es lo que el script hace por vos, paso a paso, si lo hicieras click-a-click:
>
> | Paso del script | Servicio AWS | Que estarias haciendo en Console |
> |---|---|---|
> | 1) `s3api create-bucket` | **S3** | `S3 > Create bucket` con nombre `ml-training-tfstate-<sufijo>` en `us-east-1`. Es donde Terraform va a guardar el archivo `.tfstate` (el "mapa" de que recursos AWS pertenecen a esta infra). |
> | 2) `put-bucket-versioning` | **S3** | Dentro del bucket → `Properties > Bucket Versioning > Enable`. Guarda cada cambio del `.tfstate` como version nueva — si un `terraform apply` rompe el state, podes restaurar la version anterior. |
> | 3) `put-bucket-encryption` + `put-public-access-block` | **S3** | `Properties > Default encryption > AES-256` y `Permissions > Block public access > All ON`. El state file tiene secrets en plano (passwords RDS, etc.); cifrarlo y bloquear acceso publico es mandatorio. |
> | 4) `dynamodb create-table` | **DynamoDB** | `DynamoDB > Tables > Create table` con nombre `ml-training-tflock`, **Partition key**: `LockID` (String), **Capacity**: On-demand. Cuando alguien corre `terraform apply`, escribe una fila aca para "lockear" el state; si otro intenta apply al mismo tiempo, falla con `state locked`. Asi evitamos que dos personas modifiquen la infra a la vez y se corrompa el state. |
> | 5) `create-service-linked-role` (x3) | **IAM** | NO hay wizard "Create role" para esto — las **Service Linked Roles (SLR)** son especiales. En Console aparecen en `IAM > Roles` ya creadas (`AWSServiceRoleForEC2Spot`, `AWSServiceRoleForECS`, `AWSServiceRoleForBatch`) cuando AWS las genera **automaticamente** al primer uso del servicio. El script las pre-crea via API (`iam:CreateServiceLinkedRole`) para que el primer `terraform apply` (que las asume implicitamente al lanzar Spot/ECS/Batch) no falle con `role does not exist yet`. Son distintas a los roles "normales" porque solo pueden ser asumidas por el service AWS exacto que las nombra (no por usuarios), y AWS las gestiona internamente. |
>
> **Por que no lo haces desde Console**: estos 5 recursos son la "base que sostiene a Terraform mismo". Si los crearas a mano y los borraras sin querer, perderias el state entero y Terraform no sabria que recursos AWS le pertenecen (los huerfanaria, pagandolos sin poder destruirlos). El script las hace **idempotentes** (re-ejecutar es seguro) y deja un audit trail claro.

```bash
#!/usr/bin/env bash
# infra/bootstrap.sh — Bootstrap del backend Terraform.
# UNA VEZ por cuenta + region. Idempotente.

set -euo pipefail

PROJECT="${PROJECT:-ml-training}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
# Mismas convenciones que Capítulo 3.5 (ACCOUNT_ID / ACCOUNT_SUFFIX) — si el
# usuario ya las exporto en su sesion, las reusamos; sino las calculamos.
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ACCOUNT_SUFFIX="${ACCOUNT_SUFFIX:-${ACCOUNT_ID: -6}}"
TFSTATE_BUCKET="${PROJECT}-tfstate-${ACCOUNT_SUFFIX}"
LOCK_TABLE="${PROJECT}-tflock"

# 1) S3 bucket (idempotente)
if ! aws s3api head-bucket --bucket "$TFSTATE_BUCKET" 2>/dev/null; then
    if [[ "$REGION" == "us-east-1" ]]; then
        aws s3api create-bucket --bucket "$TFSTATE_BUCKET" --region "$REGION"
    else
        aws s3api create-bucket --bucket "$TFSTATE_BUCKET" --region "$REGION" \
            --create-bucket-configuration "LocationConstraint=$REGION"
    fi
fi

# 2) Versioning + 3) Encryption + Public access block
aws s3api put-bucket-versioning --bucket "$TFSTATE_BUCKET" \
    --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$TFSTATE_BUCKET" \
    --server-side-encryption-configuration '{
      "Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]
    }'
aws s3api put-public-access-block --bucket "$TFSTATE_BUCKET" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# 4) DynamoDB lock
if ! aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "$REGION" >/dev/null 2>&1; then
    aws dynamodb create-table --table-name "$LOCK_TABLE" \
        --attribute-definitions AttributeName=LockID,AttributeType=S \
        --key-schema AttributeName=LockID,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST --region "$REGION" >/dev/null
    aws dynamodb wait table-exists --table-name "$LOCK_TABLE" --region "$REGION"
fi

# 5) Service Linked Roles (errores se ignoran si ya existen)
aws iam create-service-linked-role --aws-service-name spot.amazonaws.com   2>/dev/null || true
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com    2>/dev/null || true
aws iam create-service-linked-role --aws-service-name batch.amazonaws.com  2>/dev/null || true

echo "==> BOOTSTRAP COMPLETADO"
echo "    bucket=$TFSTATE_BUCKET  lock=$LOCK_TABLE  region=$REGION"
```

> Variantes mas completas del script pueden agregar logging
> detallado, validaciones de region, y exports sugeridos. El bloque de
> arriba es el minimo que la guia necesita para el resto del flujo.

## 2.3 Ejecutar UNA vez

```bash
# Desde la raiz del repo (WSL Ubuntu o bash nativo Linux/Mac)
cd /mnt/c/Users/CarlosAlexanderAbant/Documents/Proyectos/ml_random_forest/ml_training

# Crear el directorio infra/ si no existe
mkdir -p infra

# Verificar que el script existe (lo creaste en §2.2)
ls -la infra/bootstrap.sh
# Si no existe -> volver a §2.2 y pegar el contenido en infra/bootstrap.sh

# Dar permiso ejecutable + ejecutar
chmod +x infra/bootstrap.sh
bash infra/bootstrap.sh
```

Salida esperada (el script es silencioso; solo imprime el resumen final):

```
==> BOOTSTRAP COMPLETADO
    bucket=ml-training-tfstate-789012  lock=ml-training-tflock  region=us-east-1
```

Si NO ves esa linea final, algun `aws` command fallo silenciosamente
arriba (revisa con `bash -x infra/bootstrap.sh` para verlo paso a paso).

**Si re-ejecutas el script**: es idempotente. Va a decir "Ya existe.
Skip create." en los pasos donde el recurso ya esta, y los SLR no
fallan (estan filtrados con `2>$null`).

## 2.4 Verificacion post-bootstrap (4 checks)

Despues de que el script termine, valida que TODO quedo bien:

```bash
# Variables (recreadas para que esta seccion sea standalone)
export PROJECT="ml-training"
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export ACCOUNT_SUFFIX="${ACCOUNT_ID: -6}"
TFSTATE_BUCKET="${PROJECT}-tfstate-${ACCOUNT_SUFFIX}"
LOCK_TABLE="${PROJECT}-tflock"

# Check 1: bucket existe y tiene versioning ON
aws s3api get-bucket-versioning --bucket "$TFSTATE_BUCKET" --query Status --output text
# Esperado: Enabled

# Check 2: bucket tiene encryption AES256
aws s3api get-bucket-encryption --bucket "$TFSTATE_BUCKET" \
    --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' \
    --output text
# Esperado: AES256

# Check 3: bucket bloquea acceso publico
aws s3api get-public-access-block --bucket "$TFSTATE_BUCKET" \
    --query 'PublicAccessBlockConfiguration.BlockPublicAcls' --output text
# Esperado: True

# Check 4: DynamoDB tabla activa
aws dynamodb describe-table --table-name "$LOCK_TABLE" \
    --query 'Table.TableStatus' --output text
# Esperado: ACTIVE
```

Si los 4 dan los valores esperados, **el bootstrap esta OK**. Si alguno
falla, leelo despacio: la causa mas comun es region mal seteada
(creaste en `us-east-1` pero estas consultando con perfil que default
es otra).

## 2.5 OIDC provider para GitHub Actions (pre-Terraform)

Mismo motivo que 2.1: el OIDC provider tiene que existir antes de que
Terraform pueda crear los IAM roles que confian en el. Lo creamos a mano.

> **Equivalente en AWS Console** — esto es lo que el script crea, si lo hicieras click-a-click:
>
> | Paso del script | Servicio AWS | Que estarias haciendo en Console |
> |---|---|---|
> | `create-open-id-connect-provider` | **IAM** | `IAM > Identity providers > Add provider`. **Provider type**: OpenID Connect. **Provider URL**: `https://token.actions.githubusercontent.com` (clickear `Get thumbprint` — Console lo deriva sola). **Audience**: `sts.amazonaws.com`. **Thumbprint**: `6938fd4d98bab03faadb97b34396831e3780aea1`. **Warning** — desde **mid-2023** AWS valida internamente el certificado de GitHub contra una CA pinneada, asi que el thumbprint pasa a ser un campo "vestigial" — la API lo sigue requiriendo, pero AWS no lo usa para validar. El script lo pasa hardcodeado por compatibilidad. Si la API te rechaza ese valor en el futuro, basta con cualquier hex valido de 40 chars. |
>
> **Que es esto conceptualmente**: es el "puente de confianza" entre GitHub Actions y tu cuenta AWS. Cuando un workflow corre en GitHub, GH emite un **JWT firmado** que dice "este job corre en el repo X, branch Y, ambiente Z". El provider OIDC le dice a AWS: "confio en los JWT firmados por `token.actions.githubusercontent.com`". Despues, los IAM Roles del modulo `cicd` (Parte 3.11) declaran su trust policy: "permito que asuma este rol cualquiera que venga con un JWT del repo `mi-org/ml_training` en branch `main`". Resultado: GHA puede hacer `aws ecr push` **sin necesitar un Access Key + Secret Key guardado como secret** (que sera la pesadilla de seguridad clasica).
>
> **Por que es shared a nivel cuenta**: AWS solo permite UN OIDC provider por URL en toda la cuenta. Si ya lo creaste para otro repo (ej: `mi-otra-app`), reusalo — no recrees ni borres. Lo que **distingue** que repo puede asumir que rol es el `sub:` claim del trust policy (definido en Parte 3.11.2), no el provider en si.

### Script `infra/bootstrap-oidc.sh`

Crear el archivo `infra/bootstrap-oidc.sh` con el contenido siguiente
(si ya existe, comparar con `diff` antes de sobreescribir):

```bash
#!/usr/bin/env bash
# infra/bootstrap-oidc.sh — OIDC provider de GitHub Actions. UNA VEZ por cuenta.
set -euo pipefail

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
PROVIDER="arn:aws:iam::${ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"

if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER" >/dev/null 2>&1; then
    echo "OIDC provider ya existe: $PROVIDER"
else
    aws iam create-open-id-connect-provider \
        --url "https://token.actions.githubusercontent.com" \
        --client-id-list "sts.amazonaws.com" \
        --thumbprint-list "6938fd4d98bab03faadb97b34396831e3780aea1" >/dev/null
    echo "OIDC provider creado: $PROVIDER"
fi
```

### Ejecutar UNA vez

```bash
chmod +x infra/bootstrap-oidc.sh
bash infra/bootstrap-oidc.sh
```

### Verificacion

```bash
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
aws iam get-open-id-connect-provider \
    --open-id-connect-provider-arn "arn:aws:iam::${ACCOUNT}:oidc-provider/token.actions.githubusercontent.com" \
    --query 'Url' --output text
# Esperado: https://token.actions.githubusercontent.com
```

> **Atencion**: el OIDC provider es **shared a nivel cuenta**. Si tu
> cuenta de AWS ya lo usaba para otro repo, no lo recrees — verifica
> que existe con el check de arriba y segui. La condicion `aud` del
> trust policy (que se define en Parte 3.12 del modulo `cicd`) es lo
> que limita el acceso a tu repo especifico.

## 2.6 Snapshot del estado bootstrapped (commit + tag)

El bootstrap es irreversible y no esta versionado en Terraform. Marcalo
con un commit + tag para tener un punto de retorno claro:

```bash
# Por ahora solo los scripts. terraform.tfvars (con valores sensibles)
# se agrega al .gitignore en Parte 3.2.4 — no existe todavia.
git add infra/bootstrap.sh infra/bootstrap-oidc.sh
git commit -m "infra: bootstrap scripts para S3 tfstate + DDB lock + OIDC provider"
git tag -a "infra/bootstrap-done" -m "Bootstrap ejecutado en cuenta $ACCOUNT_ID region $AWS_DEFAULT_REGION"
git push origin main --tags   # opcional pero recomendado
```

A partir de este punto, **toda la infra es Terraform**. Los `.sh` del
bootstrap no se vuelven a tocar salvo que destruyas la cuenta entera
(§8.7).

---

# Parte 3 — Modulos Terraform

> **Filosofia de la Parte 3**: cada modulo es una caja con interface
> publica (variables.tf + outputs.tf). El `envs/prod/main.tf` solo
> compone — no contiene `resource "aws_..."` directos. Esto te deja:
>
> - Tocar `modules/batch/` sin re-aplicar el resto.
> - Crear `envs/dev/` o `envs/staging/` copiando `envs/prod/` y
>   cambiando solo `terraform.tfvars`.
> - Hacer reviews de PR donde el diff de un cambio chico es chico
>   (no 200 lineas mezcladas).

## 3.1 Layout — el arbol de archivos

Al final de la Parte 3 tu repo tiene este arbol (los `.sh` del bootstrap
ya estan desde la Parte 2):

```
ml_training/
├── infra/
│   ├── bootstrap.sh                       # Parte 2.2
│   ├── bootstrap-oidc.sh                  # Parte 2.5
│   ├── envs/
│   │   └── prod/
│   │       ├── versions.tf                 # 3.2.1
│   │       ├── backend.tf                  # 3.2.2
│   │       ├── variables.tf                # 3.2.3
│   │       ├── terraform.tfvars            # 3.2.4 (gitignored)
│   │       ├── main.tf                     # 3.2.5
│   │       └── outputs.tf                  # 3.2.6
│   ├── modules/
│   │   ├── _shared/                        # 3.4.5 (trust policies JSON compartidos)
│   │   │   ├── README.md
│   │   │   ├── assume-ecs-tasks.json
│   │   │   ├── assume-lambda.json
│   │   │   ├── assume-ec2.json
│   │   │   ├── assume-batch-service.json
│   │   │   └── assume-github-oidc.json.tftpl
│   │   ├── network/                        # 3.3 (split: main.tf + security_groups.tf)
│   │   ├── storage/                        # 3.4
│   │   ├── mlflow/                         # 3.5 (split: main.tf + alb.tf + ecs.tf + iam.tf + rds.tf)
│   │   ├── reports/                        # 3.6
│   │   ├── batch/                          # 3.7
│   │   ├── monitoring/                     # 3.8
│   │   ├── lambdas/                        # 3.9
│   │   ├── scheduler/                      # 3.10
│   │   ├── cicd/                           # 3.11
│   │   └── consumer-iam/                   # 3.11.5 (Patch 13.5 — rol OIDC repo consumer)
│   └── lambdas/                            # Codigo Python de las Lambdas
│       ├── dispatcher.py                   # 3.9.5
│       ├── notifier.py                     # 3.9.6
│       └── scheduler.py                    # 3.10.4
├── docker/
│   ├── mlflow/Dockerfile                   # ya existe (custom MLflow)
│   ├── reports/Dockerfile                  # 3.6.5 (nginx + s3-sync sidecar)
│   └── nginx-reports.conf                  # ya existe (local) + version cloud (3.6.6)
├── (resto del proyecto: src/, main.py, Dockerfile, ...)
```

Crear el esqueleto vacio:

```bash
# Desde la raiz del repo
dirs=(
    "infra/envs/prod"
    "infra/modules/_shared"
    "infra/modules/network"
    "infra/modules/storage"
    "infra/modules/mlflow"
    "infra/modules/reports"
    "infra/modules/batch"
    "infra/modules/monitoring"
    "infra/modules/lambdas"
    "infra/modules/scheduler"
    "infra/modules/cicd"
    "infra/modules/consumer-iam"
    "infra/lambdas"
    "docker/reports"
)
for d in "${dirs[@]}"; do mkdir -p "$d"; done

# Verificar
find infra/ docker/reports -type d
```

## 3.2 `envs/prod/` — la composicion

### 3.2.1 `infra/envs/prod/versions.tf`

Locks de versiones — toda la guia esta probada con estas versiones. Si
las cambias, vas a tener que ajustar sintaxis (p.ej. `for_each` map en
v5 vs v4 del provider AWS).

```hcl
terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "Terraform"
      Env       = "prod"
    }
  }
}
```

### 3.2.2 `infra/envs/prod/backend.tf`

```hcl
terraform {
  backend "s3" {
    # Valores se inyectan desde -backend-config en `terraform init`.
    # Asi el bucket no queda hardcoded en el repo (depende del account suffix).
    encrypt = true
  }
}
```

Como se usa (Parte 4.2):

```bash
BUCKET="${PROJECT}-tfstate-${ACCOUNT_SUFFIX}"
LOCK="${PROJECT}-tflock"
terraform init \
    -backend-config="bucket=${BUCKET}" \
    -backend-config="key=envs/prod/terraform.tfstate" \
    -backend-config="region=${AWS_DEFAULT_REGION}" \
    -backend-config="dynamodb_table=${LOCK}"
```

### 3.2.3 `infra/envs/prod/variables.tf`

```hcl
variable "project" {
  description = "Slug del proyecto (prefijo de todos los recursos)."
  type        = string
  default     = "ml-training"
}

variable "region" {
  description = "Region AWS para todo el deployment."
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "CIDR de la VPC. /16 da espacio para 65k IPs."
  type        = string
  default     = "10.20.0.0/16"
}

variable "alert_email" {
  description = "Email que recibe notificaciones SNS (job FAILED, MAPE high)."
  type        = string
}

variable "github_org" {
  description = "Organizacion / usuario GitHub que aloja el repo (para OIDC trust)."
  type        = string
}

variable "github_repo" {
  description = "Nombre del repo (sin la org). Para trust policy OIDC."
  type        = string
}

variable "varieties_allowed" {
  description = "Allow-list defensivo para el Lambda dispatcher (rechaza submits con variety no listada). NO define las variedades del modelo: la verdad esta en las hojas del Excel (data/BD_HISTORICO_ACUMULADO.xlsx) y se descubre dinamicamente con src/step_01_load/data_loader.py::list_varieties(). Esta lista solo previene typos en `aws lambda invoke`."
  type        = list(string)
  default     = ["POP", "JUPITER", "VENTURA", "SEKOYA", "ALLISON", "STELLA"]
}

variable "spot_max_vcpus" {
  description = "Maximo de vCPUs simultaneas en la queue Spot."
  type        = number
  default     = 16
}

variable "ondemand_max_vcpus" {
  description = "Maximo de vCPUs simultaneas en la queue On-Demand (solo prod_xl)."
  type        = number
  default     = 16
}

variable "batch_instance_type" {
  description = "Tipo de instancia EC2 que arranca Batch."
  type        = string
  default     = "c6i.2xlarge"
}

variable "rds_instance_class" {
  description = "Clase RDS para MLflow backend."
  type        = string
  default     = "db.t4g.micro"
}

variable "mlflow_image_tag" {
  description = "Tag de la imagen MLflow en ECR (build manual una vez)."
  type        = string
  default     = "v3.12.0"
}

variable "reports_image_tag" {
  description = "Tag de la imagen reports (nginx + s3-sync) en ECR."
  type        = string
  default     = "stable"
}

variable "trainer_image_tag" {
  description = "Tag de la imagen del trainer. CI/CD lo sobreescribe por commit SHA."
  type        = string
  default     = "latest"
}

variable "mape_alarm_threshold" {
  description = "Umbral de MAPE (%) para disparar alarma CloudWatch."
  type        = number
  default     = 25
}

variable "log_retention_days" {
  description = "Dias que CloudWatch retiene logs."
  type        = number
  default     = 14
}

variable "work_start_hour_local" {
  description = "Hora local de arranque del scheduler (PET, UTC-5)."
  type        = number
  default     = 8
}

variable "work_end_hour_local" {
  description = "Hora local de apagado del scheduler."
  type        = number
  default     = 12
}
```

### 3.2.4 `infra/envs/prod/terraform.tfvars` (NO COMMITEAR)

```hcl
alert_email = "abantodca@gmail.com"
github_org  = "abantodca"
github_repo = "ml_training"
```

Agregar a `.gitignore`:

```bash
cat >> .gitignore <<'EOF'

# Terraform
**/terraform.tfvars
**/.terraform/
**/.terraform.lock.hcl
*.tfstate
*.tfstate.*
.terraformrc
terraform.rc

# Lambdas .zip (Terraform los crea desde Python source)
infra/modules/lambdas/*.zip
infra/modules/scheduler/*.zip
EOF
```

### 3.2.5 `infra/envs/prod/main.tf` (esqueleto — crece incrementalmente)

> **Como leer esta seccion**: a diferencia de los `variables.tf` /
> `outputs.tf` que se pegan completos, este `main.tf` se **construye
> en partes** a medida que avanzas por la guia. Aca pegas solo el
> esqueleto (los `data` sources). Cada `module "X" {}` se va
> apendeando al final del archivo cuando llegues a la seccion del
> modulo correspondiente:
>
> | Bloque                | Se agrega en | Modulo creado en |
> |---|---|---|
> | `module "network"`    | §3.3.4       | §3.3             |
> | `module "storage"`    | §3.4.4       | §3.4             |
> | `module "mlflow"`     | §3.5.4       | §3.5             |
> | `module "reports"`    | §3.6.7       | §3.6             |
> | `module "batch"`      | §3.7.5       | §3.7             |
> | `module "monitoring"` | §3.8.4       | §3.8             |
> | `module "lambdas"`    | §3.9.7       | §3.9             |
> | `module "scheduler"`  | §3.10.5      | §3.10            |
> | `module "cicd"`       | §3.11.4      | §3.11            |
>
> **Por que incremental y no de un saque**: cada `module "X" {}`
> referencia outputs de modulos anteriores (e.g. `module.network.vpc_id`).
> Si pegas el `main.tf` completo antes de crear los modulos, `terraform
> validate` truena con "module not found" en cada uno. Apendear por
> capas mantiene el archivo siempre **valido** al terminar cada seccion
> — podes correr `terraform fmt` / `validate` checkpoint por checkpoint.
> La verificacion final integrada esta en §3.12.

Pegar **solo** este contenido inicial:

```hcl
# Datos compartidos
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# OIDC provider de GitHub (creado en Parte 2.5, NO creado por Terraform).
# Si saltaste §2.5, este `data` falla con "no resource found" en plan.
# Pre-check antes de `terraform plan`:
#   aws iam list-open-id-connect-providers --query 'OpenIDConnectProviderList[?contains(Arn,`token.actions.githubusercontent.com`)]'
# Si devuelve [], correr `bash infra/bootstrap-oidc.sh` (§2.5).
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}
```

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `data "aws_caller_identity"` | **IAM / STS** | `IAM > Dashboard` muestra arriba a la derecha tu **Account ID** de 12 digitos. Read-only — solo "pregunta a AWS quien soy" via `sts:GetCallerIdentity`. |
> | `data "aws_region"` | **Region picker** | El selector de region arriba a la derecha (e.g. `us-east-1`). Tambien read-only. |
> | `data "aws_iam_openid_connect_provider"` | **IAM** | `IAM > Identity providers` → veras `token.actions.githubusercontent.com` (creado por `bootstrap-oidc.sh` en §2.5). El `data` lo "lee" para que `module.cicd` (§3.11) pueda asignar trust policies a los roles GHA sin hardcodear el ARN. |
>
> **Conceptualmente — `data` vs `resource`**: `data` source = lectura
> de algo que **ya existe** (creado fuera de Terraform o por otro
> stack). `resource` = Terraform **gestiona el ciclo de vida**
> (create/update/destroy). Por eso el OIDC provider esta como `data`:
> lo creamos a mano en §2.5 con `bootstrap-oidc.sh` para que cualquier
> `terraform destroy` accidental no te tire la confianza GHA-AWS
> (recrearlo cuesta rotar `vars.AWS_GHA_DEPLOY_ROLE_ARN` y arreglar
> branch protection — friccion innecesaria).

### 3.2.6 `infra/envs/prod/outputs.tf`

```hcl
output "alb_dns" {
  description = "DNS publico del ALB (MLflow + Reports)."
  value       = module.mlflow.alb_dns
}

output "tracking_uri" {
  description = "URL completa para MLFLOW_TRACKING_URI."
  value       = module.mlflow.tracking_uri
}

output "ecr_trainer_url" {
  description = "URL del repo ECR del trainer (para docker push)."
  value       = module.storage.ecr_trainer_url
}

output "ecr_mlflow_url" {
  description = "URL del repo ECR del MLflow custom."
  value       = module.storage.ecr_mlflow_url
}

output "ecr_reports_url" {
  description = "URL del repo ECR del reports nginx."
  value       = module.storage.ecr_reports_url
}

output "data_bucket" {
  value = module.storage.data_bucket
}

output "artifacts_bucket" {
  value = module.storage.artifacts_bucket
}

output "job_queue_spot" {
  value = module.batch.job_queue_spot
}

output "job_queue_ondemand" {
  value = module.batch.job_queue_ondemand
}

output "job_definition_name" {
  value = module.batch.job_definition_name
}

output "dispatcher_function_name" {
  value = module.lambdas.dispatcher_function_name
}

output "sns_topic_arn" {
  value = module.monitoring.sns_topic_arn
}

output "gha_deploy_role_arn" {
  description = "Role que asume GitHub Actions para `terraform apply`."
  value       = module.cicd.gha_deploy_role_arn
}

output "gha_train_role_arn" {
  description = "Role que asume GitHub Actions para invocar Lambda dispatcher."
  value       = module.cicd.gha_train_role_arn
}

# Patch 13.5
output "consumer_role_arn" {
  description = "Role que asume el repo consumer (ml-serving) via OIDC para descargar artifacts."
  value       = module.consumer_iam.consumer_role_arn
}
```

## 3.3 `modules/network/` — VPC + subnets + NAT + SGs

Single-AZ a proposito (Sec 0.2 lockeada). El SG matrix es:

- `sg_alb`: ingress :80 from 0.0.0.0/0 (futuro: WAF + TLS en Parte 10.1)
- `sg_mlflow`: **dos reglas de ingress desde `sg_alb`**: :5000 (MLflow
  task escucha ahi) y :80 (modulo reports reusa este SG porque comparte
  ECS cluster; el container nginx escucha en :80). Nada desde 0.0.0.0/0.
- `sg_rds`: ingress :5432 from `sg_mlflow` + `sg_batch` (Batch necesita
  conectar a RDS para registrar runs via MLflow Python client)
- `sg_batch`: egress 443 a internet (S3, ECR, MLflow ALB)

### 3.3.1 `modules/network/variables.tf`

```hcl
variable "project" { type = string }
variable "vpc_cidr" { type = string }
```

### 3.3.2 `modules/network/main.tf`

Pegar los bloques siguientes **uno a continuacion del otro** en el
mismo archivo `modules/network/main.tf`. La separacion en sub-bloques
con `### 3.3.2.X` es solo para que puedas leer el "por que" de cada
pieza sin perderte; el archivo final es la concatenacion de los 5
bloques.

#### 3.3.2.a — Discovery de AZs + VPC

Necesitamos saber que AZs tiene esta region disponibles (sin
hardcodear `us-east-1a/b`, asi la guia funciona en cualquier region).
La VPC propia evita choques con default-VPC.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `data "aws_availability_zones"` | **EC2** | `EC2 > Account attributes > Availability Zones`. Lista las AZs disponibles (ej: `us-east-1a`, `us-east-1b`, `us-east-1c`...). El `data` es un "read-only lookup" — no crea nada, solo lee. |
> | `aws_vpc.main` | **VPC** | `VPC > Your VPCs > Create VPC`. **Name tag**: `ml-training-vpc`. **IPv4 CIDR**: `10.20.0.0/16` (var.vpc_cidr). **Tenancy**: default. **DNS hostnames + DNS resolution**: enabled. Una VPC es tu "red privada en AWS" — todo lo demas (subnets, EC2, RDS, Fargate) vive adentro. |
>
> **Conceptualmente**: una VPC es como rentar un edificio entero — adentro vos decidis los pisos (subnets), pasillos (route tables), y porteros (security groups). El CIDR `10.20.0.0/16` da 65536 IPs disponibles para repartir entre subnets. Usamos una VPC propia (no la default) para aislamiento + no chocar con recursos preexistentes de la cuenta.

```hcl
data "aws_availability_zones" "available" { state = "available" }

locals {
  # Solo 2 AZs (la "AZ secundaria" se reserva por RDS multi-AZ futuro)
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.project}-vpc" }
}
```

#### 3.3.2.b — Subnets (public + private, x2 AZs)

2 public (ALB y NAT) + 2 private (Fargate, Batch, RDS). El offset `+10`
en cidrsubnet evita que los rangos public y private se toquen — facilita
debugging cuando ves una IP en CloudTrail.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_subnet.public[0..1]` | **VPC** | `VPC > Subnets > Create subnet`. **VPC**: la que creaste arriba. **Name**: `ml-training-public-0` / `-1`. **AZ**: una distinta por subnet (`us-east-1a`, `us-east-1b`). **CIDR**: `10.20.0.0/24` y `10.20.1.0/24`. Despues edit → `Auto-assign IPv4`: ON. |
> | `aws_subnet.private[0..1]` | **VPC** | Mismo wizard pero **CIDR**: `10.20.10.0/24` y `10.20.11.0/24`. **Auto-assign IPv4**: OFF. |
>
> **Conceptualmente** — la distincion public/private es CRITICA: 
> - **Public subnet**: tiene una ruta `0.0.0.0/0 → IGW`. Cualquier recurso aqui puede salir a Internet Y ser alcanzable desde Internet (con su IP publica). Aca vive el ALB (necesita aceptar trafico de Internet) y la NAT Gateway.
> - **Private subnet**: tiene una ruta `0.0.0.0/0 → NAT`. Los recursos aqui pueden salir a Internet (para `docker pull` de ECR, log a CloudWatch, etc.) pero **NO son alcanzables desde Internet**. Aca viven MLflow Fargate, los jobs de Batch y RDS — todo lo "sensible" sin exposicion publica.
> - **Por que 2 AZs**: requisito de ALB (no acepta crearse con 1 subnet sola — necesita 2 en AZs distintas para tolerancia a fallos). Aunque el resto sea single-AZ a proposito (NAT, RDS), las 2 subnets de cada lado son obligatorias por el ALB.

```hcl
resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index) # 10.20.0.0/24, 10.20.1.0/24
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.project}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10) # 10.20.10.0/24, 10.20.11.0/24
  availability_zone = local.azs[count.index]
  tags              = { Name = "${var.project}-private-${count.index}" }
}
```

#### 3.3.2.c — Internet Gateway + NAT (single, en public[0])

IGW para que las public subnets salgan a Internet. NAT (single, no HA)
para que las private salgan SIN ser alcanzables. NAT es **single porque
es el item caro** (~$32/mes); HA exigiria 2 NATs = $64/mes.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_internet_gateway.igw` | **VPC** | `VPC > Internet gateways > Create internet gateway`. **Name**: `ml-training-igw`. Despues `Actions > Attach to VPC > [tu VPC]`. Es el "portero de salida" para que cualquier IP publica de tu VPC pueda hablar con Internet. |
> | `aws_eip.nat` | **EC2** | `EC2 > Elastic IPs > Allocate Elastic IP address`. Es una IP publica fija — necesaria porque la NAT Gateway debe tener una IP estable para que el trafico de salida siempre se vea con el mismo origen. |
> | `aws_nat_gateway.main` | **VPC** | `VPC > NAT gateways > Create NAT gateway`. **Subnet**: la public-0 (tiene que estar en una subnet publica para acceder al IGW). **Elastic IP**: la que acabas de allocar. **Connectivity type**: Public. |
>
> **Conceptualmente — por que IGW Y NAT**: parece redundante pero hacen cosas opuestas. **IGW** permite trafico **bidireccional** (Internet ↔ recurso con IP publica) — sirve para el ALB. **NAT** permite **solo trafico saliente** (recurso privado → Internet → respuesta vuelve) — sirve para Fargate/Batch en private subnets que necesitan `docker pull` pero no deben aceptar conexiones entrantes. Sin NAT, los jobs de Batch no podrian pullear imagenes de ECR ni postear runs a CloudWatch.
>
> **Por que NAT es el item caro**: $32/mes solo por estar prendida + $0.045/GB de trafico procesado. Si tu trainer descarga 5 GB de paquetes Python en cada job + log de 1 GB → $0.27 por job. En Parte 10.3 hay un plan para reemplazarla con **VPC Endpoints** (gratis para S3/ECR), que reduce el costo a casi cero.

```hcl
resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project}-igw" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${var.project}-nat-eip" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = "${var.project}-nat" }
  depends_on    = [aws_internet_gateway.igw]
}
```

#### 3.3.2.d — Route tables

Public RT: 0.0.0.0/0 → IGW. Private RT: 0.0.0.0/0 → NAT. La
asociacion x2 vincula las subnets a su RT correspondiente.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_route_table.public` | **VPC** | `VPC > Route tables > Create route table`. **Name**: `ml-training-rt-public`. **VPC**: la tuya. Despues editar `Routes > Edit routes > Add route`: **Destination**: `0.0.0.0/0`, **Target**: Internet Gateway → seleccionar el IGW. |
> | `aws_route_table.private` | **VPC** | Mismo wizard, **Name**: `ml-training-rt-private`. **Route**: `0.0.0.0/0` → NAT Gateway. |
> | `aws_route_table_association.*` | **VPC** | En cada subnet: `Subnet > Edit route table association > [seleccionar RT]`. Esto le dice a cada subnet "para salir a Internet, usa este camino". |
>
> **Conceptualmente**: las route tables son el "GPS" de la VPC. Toda subnet **tiene** una RT asociada (si no le ponés ninguna, hereda la "main RT" de la VPC). La diferencia entre public y private subnet es 100% en la route table — una subnet es "public" PORQUE su RT apunta `0.0.0.0/0 → IGW`, no por nada en la subnet misma.

```hcl
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  tags = { Name = "${var.project}-rt-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  tags = { Name = "${var.project}-rt-private" }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}
```

#### 3.3.2.e — Security Groups (4: alb, mlflow, batch, rds)

4 SGs en cascada (alb es la unica que acepta 0.0.0.0/0; las otras
solo aceptan trafico desde la anterior, formando una cadena de
defense-in-depth):

- `sg-alb`: Internet → :80.
- `sg-mlflow`: sg-alb → :5000 (MLflow) y :80 (reports). Compartido por
  ambos services porque ambos viven detras del mismo ALB.
- `sg-batch`: solo egress (el trainer pulea de S3/ECR, escribe logs,
  postea a MLflow ALB). No acepta ingress de ningun lado.
- `sg-rds`: 5432 desde sg-mlflow + sg-batch.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_security_group.alb` | **VPC** | `VPC > Security groups > Create security group`. **Name**: `ml-training-sg-alb`. **VPC**: la tuya. **Inbound rules > Add rule**: Type=HTTP, Source=Anywhere-IPv4 (`0.0.0.0/0`). **Outbound rules**: All traffic → 0.0.0.0/0 (default). |
> | `aws_security_group.mlflow` | **VPC** | Mismo wizard. **Inbound rules**: dos reglas → (1) Custom TCP :5000 con Source=`sg-alb` (escribis el ID, no un CIDR); (2) HTTP :80 con Source=`sg-alb`. |
> | `aws_security_group.batch` | **VPC** | **Inbound rules**: **vacio** (nadie debe poder conectarse a los jobs). **Outbound**: All traffic. |
> | `aws_security_group.rds` | **VPC** | **Inbound rules**: dos reglas → (1) PostgreSQL :5432 con Source=`sg-mlflow`; (2) PostgreSQL :5432 con Source=`sg-batch`. **Outbound**: vacio (RDS no necesita salir a nada). |
>
> **Conceptualmente — SGs son firewalls "stateful" a nivel recurso**:
> - "**Stateful**" = si permitis trafico ENTRANTE, la respuesta saliente se permite **automaticamente** (a diferencia de NACLs que son stateless y requieren reglas duplicadas).
> - El truco potente: en `Source` podes poner **OTRO security group** en vez de un CIDR. Eso dice "permite trafico desde cualquier recurso que tenga este SG", sin importar su IP. Asi `sg-rds` acepta a `sg-mlflow` y `sg-batch` aunque sus IPs cambien (Fargate las reasigna en cada deploy).
> - Es una **cadena de defense-in-depth**: Internet → ALB → MLflow → RDS. Si alguien rompe el ALB, todavia no puede llegar a RDS directo (no esta en la "lista de invitados" de `sg-rds`). Bloqueamos lateral movement a nivel red.

```hcl
resource "aws_security_group" "alb" {
  name        = "${var.project}-sg-alb"
  description = "ALB: 80/HTTP desde Internet (TLS futuro en Parte 10.1)"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "mlflow" {
  name   = "${var.project}-sg-mlflow"
  vpc_id = aws_vpc.main.id

  ingress {
    description     = "MLflow server desde ALB"
    from_port       = 5000
    to_port         = 5000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  ingress {
    description     = "Reports nginx desde ALB"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "batch" {
  name   = "${var.project}-sg-batch"
  vpc_id = aws_vpc.main.id

  egress {
    description = "Egress libre (S3, ECR, MLflow ALB, CloudWatch Logs)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "rds" {
  name   = "${var.project}-sg-rds"
  vpc_id = aws_vpc.main.id

  ingress {
    description     = "Postgres desde MLflow Fargate"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.mlflow.id]
  }
  ingress {
    description     = "Postgres desde Batch (trainer logging directo)"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.batch.id]
  }
}
```

> **Checkpoint despues de pegar los 5 bloques**: ejecuta
> `terraform fmt infra/modules/network/main.tf` para confirmar que el
> archivo es sintacticamente valido. Si reformatea, OK; si imprime
> error de parse, falta pegar un `}` o uniste dos resources sin
> separador.

### 3.3.3 `modules/network/outputs.tf`

```hcl
output "vpc_id" { value = aws_vpc.main.id }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }
output "sg_alb_id" { value = aws_security_group.alb.id }
output "sg_mlflow_id" { value = aws_security_group.mlflow.id }
output "sg_batch_id" { value = aws_security_group.batch.id }
output "sg_rds_id" { value = aws_security_group.rds.id }
```

> **En consola AWS veras** despues del apply:
> - VPC → Your VPCs → `ml-training-vpc` (CIDR 10.0.0.0/16).
> - VPC → Subnets → 4 subnets: 2 public (`ml-training-public-0/1`) en
>   AZ-a/AZ-b + 2 private (`ml-training-private-0/1`).
> - VPC → NAT Gateways → 1 NAT en public[0] (`state=available`,
>   `eip=<X>`). **Cuesta ~$32/mes** + traffic — es el item caro de
>   este modulo.
> - VPC → Internet Gateways → 1 IGW.
> - VPC → Route Tables → 2 (public via IGW, private via NAT).
> - EC2 → Security Groups → 4 con tag `Project=ml-training`:
>   `sg-alb` (ingress 80 desde 0.0.0.0/0), `sg-mlflow` (ingress 80
>   desde sg-alb), `sg-batch` (egress all), `sg-rds` (ingress 5432
>   desde sg-mlflow + sg-batch).

### 3.3.4 Apendear `module "network"` en `infra/envs/prod/main.tf`

Ahora que el modulo `network` esta definido (§3.3.2) y expone sus
outputs (§3.3.3), lo **cableamos desde el env `prod`**. Pegar AL
FINAL de `infra/envs/prod/main.tf` (a continuacion del bloque
`data` de §3.2.5):

```hcl
# -------------------------------------------------------------------------
# Capa 1: Red (VPC + subnets + NAT + SGs)
# -------------------------------------------------------------------------
module "network" {
  source   = "../../modules/network"
  project  = var.project
  vpc_cidr = var.vpc_cidr
}
```

> **Checkpoint**: con esto el `main.tf` ya es valido. Podes correr
> `terraform fmt` y `terraform validate` (no `plan` todavia — falta
> el resto de los modulos). Si valida → seguir a §3.4.

---

## 3.4 `modules/storage/` — S3 buckets + ECR repos

### 3.4.1 `modules/storage/variables.tf`

```hcl
variable "project" { type = string }
```

### 3.4.2 `modules/storage/main.tf`

Pegar los bloques siguientes **uno a continuacion del otro** en el
mismo archivo `modules/storage/main.tf`. La separacion en sub-bloques
con `### 3.4.2.X` es solo para que puedas leer el "por que" de cada
pieza sin perderte; el archivo final es la concatenacion de los 5
bloques.

#### 3.4.2.a — Header: account suffix discovery

Calcula el sufijo de 6 chars que comparten todos los buckets (data,
artifacts) y el bucket de tfstate creado a mano por `bootstrap.sh`.
Equivalente bash: `${ACCOUNT: -6}`. Asi un mismo `account_id` produce
el mismo sufijo en todos los buckets — operativamente no te confundis
entre "cual era el bucket de este proyecto".

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `data "aws_caller_identity"` | **IAM / STS** | `IAM > Dashboard` muestra arriba a la derecha tu **Account ID** de 12 digitos. El `data` es read-only — no crea nada, solo "pregunta a AWS quien soy" via STS (`sts:GetCallerIdentity`). |
> | `locals.account_suffix` | — | No tiene UI: es compute puro de Terraform. Toma los ultimos 6 chars del account_id para usarlos de sufijo de bucket. |
>
> **Conceptualmente — por que un sufijo y no el nombre crudo**: los nombres de bucket S3 son **globalmente unicos** (no por cuenta, ni por region — globalmente, en TODO S3). Si dos personas hicieran `terraform apply` con `project=ml-training`, el segundo `terraform apply` fallaria con "bucket already exists". El sufijo de 6 chars del account_id hace que el bucket sea **practicamente unico** sin tener que pensar en nombres creativos, y al mismo tiempo te queda **deterministico** dentro de una misma cuenta (no random).

```hcl
data "aws_caller_identity" "current" {}

locals {
  # ${ACCOUNT: -6} en bash. substr(...,6,6) toma chars 6-11 (indices 0-based)
  # = los ultimos 6 chars de un account_id estandar de 12 digitos.
  account_suffix = substr(data.aws_caller_identity.current.account_id, 6, 6)
}
```

#### 3.4.2.b — S3 bucket `data` (input Excels) + hardening

El bucket donde subis el Excel acumulado (`BD_HISTORICO_ACUMULADO.xlsx`)
antes de cada training. Los 3 sub-recursos (versioning, encryption,
public-block) son **obligatorios** en cualquier bucket post-2023 —
defaults seguros + auditoria.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_s3_bucket.data` | **🪣 S3** | `S3 > Buckets > Create bucket`. **Name**: `ml-training-data-<sufijo>`. **Region**: `us-east-1`. **ACLs**: disabled. **Object Ownership**: ACLs disabled, Bucket owner enforced. |
> | `aws_s3_bucket_versioning.data` | **🪣 S3** | Mismo bucket → `Properties > Bucket Versioning > Edit > Enable`. Cada PUT escribe una **nueva version** en vez de pisar; podes restaurar versiones anteriores desde la consola. |
> | `aws_s3_bucket_server_side_encryption_configuration.data` | **🪣 S3** | `Properties > Default encryption > Edit > AES-256 (SSE-S3)`. **Bucket Key**: Enable (reduce costos de KMS si en el futuro migras a SSE-KMS). |
> | `aws_s3_bucket_public_access_block.data` | **🪣 S3** | `Permissions > Block public access > Edit > Block all public access`. Las 4 sub-opciones ON: bloquea ACLs publicas y bucket policies publicas, tanto las que existen como las que se intenten crear. |
>
> **Conceptualmente — por que 4 recursos Terraform para "un bucket"**: la API REST de S3 expone cada faceta del bucket como un sub-endpoint distinto (`PUT /?versioning`, `PUT /?encryption`, `PUT /?publicAccessBlock`). Terraform refleja la API 1-a-1: 1 recurso por sub-endpoint. La consola te lo presenta como tabs dentro de la "pagina de bucket", pero atras son llamadas API separadas. **Versioning** te salva si alguien sube un Excel roto (rollback en 1 click); **encryption AES-256** cumple la mayoria de policies de seguridad sin costo extra; **public access block** es la red de seguridad #1 contra "bucket S3 publico por error" — el bug clasico que hace headlines.

```hcl
resource "aws_s3_bucket" "data" {
  bucket = "${var.project}-data-${local.account_suffix}"
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
```

#### 3.4.2.c — S3 bucket `artifacts` (modelos + reportes + MLflow store) + lifecycle

Almacen central de outputs: modelos `.joblib`, JSONs de metricas,
dashboards HTML, y el "artifact store" de MLflow (cuando
`mlflow.log_artifact()` sube algo, va aca). La lifecycle policy borra
**versiones no-current** a los 90 dias para que el bill S3 no se infle
indefinidamente.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_s3_bucket.artifacts` | **🪣 S3** | `Create bucket` con nombre `ml-training-artifacts-<sufijo>`. Mismas settings que el bucket data. |
> | `aws_s3_bucket_versioning.artifacts` + `_server_side_encryption_configuration` + `_public_access_block` | **🪣 S3** | Mismos 3 sub-recursos de hardening (versioning, encryption AES-256, public access block) — identico al bloque .b. |
> | `aws_s3_bucket_lifecycle_configuration.artifacts` | **🪣 S3** | `Management > Lifecycle rules > Create lifecycle rule`. **Name**: `expire-noncurrent`. **Scope**: Apply to all objects. **Permanently delete noncurrent versions**: after **90 days**. **Abort incomplete multipart uploads**: after 7 days. |
>
> **Que guarda este bucket** — estructura tipica:
> - `artifacts/POP/final_pipeline_POP_v1.joblib` (el modelo entrenado)
> - `artifacts/POP/run_summary_POP.json` (metricas del run)
> - `reports/POP/dashboard.html` (dashboards interactivos)
> - Internamente MLflow tambien apunta sus `artifact_uri` aca.
>
> **Por que 90 dias y no 30 ni 365**: tres meses cubren un ciclo razonable de A/B testing entre modelos (cuanto tiempo querrias mirar atras para comparar un campeon contra su predecesor). **Mas corto** perderia auditoria de incidentes pasados — "el modelo de hace 2 meses que se rompio en prod, donde esta?". **Mas largo** infla el bill S3 sin valor operativo: los artifacts viejos se vuelven data fria sin uso. Nota: la lifecycle solo borra **noncurrent versions** (las versiones viejas que reemplazaste); la version actual del modelo nunca se borra automaticamente.
>
> **Por que `abort_incomplete_multipart_upload`**: cuando subis un archivo grande (~modelo de 100 MB+) y la subida se corta a la mitad, S3 te cobra storage por los chunks parciales aunque no podes ver el archivo. Esta regla los limpia a los 7 dias — barato seguro contra "fantasmas" en el bill.

```hcl
resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.project}-artifacts-${local.account_suffix}"
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 90 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}
```

#### 3.4.2.d — ECR `trainer` + lifecycle policy

El "Docker Hub privado" donde vive la imagen del trainer
(`ml-training:v0.1.0`, `:sha-abc123`, `:latest`). El job de Batch hace
`docker pull` desde aca cuando arranca. La lifecycle policy evita que
ECR acumule decenas de GB de imagenes viejas.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_ecr_repository.trainer` | **ECR** | `ECR > Private repositories > Create repository`. **Name**: `ml-training`. **Tag immutability**: `Mutable`. **Scan on push**: Enabled. **Encryption**: AES-256. |
> | `aws_ecr_lifecycle_policy.trainer` | **ECR** | `ECR > [repo trainer] > Lifecycle Policy > Edit > Add rule`. Definir 2 reglas: Priority 1 = keep last 10 tagged `v*`/`sha-*`, Priority 2 = expire untagged > 7 days. La consola muestra preview de "que imagenes se borrarian con esta regla". |
>
> **Conceptualmente — MUTABLE vs IMMUTABLE**: `Mutable` permite que la **misma tag** apunte a una imagen distinta en el futuro (ej: `latest` se mueve de la imagen vieja a la nueva en cada push). Util para CI/CD donde reusas tags como `latest`/`main`. **Pero** si alguien hace `docker pull ml-training:latest` hoy y manana, recibe imagenes **distintas**, lo cual es trampa para debugging. Por eso ademas de `latest` siempre tagueamos con el sha del commit (`sha-abc123`) — esa es la tag "real" e inmutable de facto.
>
> **Por que la lifecycle**: cada imagen de trainer pesa ~1-2 GB. Si pusheas 50 versiones sin limpiar son ~75 GB acumulados (~$7.50/mes solo por storage en ECR). La policy garantiza max ~20 GB en cualquier momento (~$2/mes). Las reglas con `rulePriority` se evaluan en orden ascendente: primero "keep last 10 tagged", lo no-matcheado pasa a la regla 2 "expire untagged > 7 days".
>
> **Sobre `scan_on_push`**: ECR analiza la imagen recien subida buscando CVEs conocidos en sus paquetes (te muestra "imagen tiene CVE-2024-XXXX en openssl") en `ECR > [repo] > Images > [imagen] > Vulnerabilities`. Util pero **NO bloquea el push** — es solo informativo. Bloquear pushes con CVEs requiere un step adicional en CI (con `aws ecr describe-image-scan-findings`).

```hcl
resource "aws_ecr_repository" "trainer" {
  name                 = var.project
  image_tag_mutability = "MUTABLE" # CI/CD reusa tag "latest" + sha
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

resource "aws_ecr_lifecycle_policy" "trainer" {
  repository = aws_ecr_repository.trainer.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 10 tagged"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v", "sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Expire untagged > 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      }
    ]
  })
}
```

#### 3.4.2.e — ECR `mlflow` (IMMUTABLE) + ECR `reports` (MUTABLE)

Dos repos mas con politicas **opuestas** de tag immutability — la
diferencia es deliberada y refleja como evolucionan los binarios.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_ecr_repository.mlflow` | **ECR** | `Create repository`. **Name**: `ml-training-mlflow`. **Tag immutability**: **`IMMUTABLE`** (importante!). Scan on push: Enabled. Encryption: AES-256. |
> | `aws_ecr_repository.reports` | **ECR** | `Create repository`. **Name**: `ml-training-reports`. **Tag immutability**: `Mutable`. Scan on push: Enabled. Encryption: AES-256. |
>
> **Por que MLflow va IMMUTABLE**: el binario de MLflow es un release oficial verificado (v3.12.0 baja de PyPI y se pinea con su hash). Si alguien pudiera sobrescribir la tag `v3.12.0` con una imagen distinta (intencional o accidentalmente), el ALB de produccion empezaria a servir una version no-auditada. **IMMUTABLE** = AWS rechaza con error cualquier push que intente reusar una tag existente. El nivel de proteccion es **a nivel API** — ni un humano con permisos de admin puede sobrescribir.
>
> **Por que reports queda MUTABLE**: el nginx + entrypoint.sh del modulo `reports` es codigo nuestro que iteramos seguido (ajustar `nginx.conf`, mejorar el `s3-sync`). Es razonable re-pushear `:latest` muchas veces. Ademas no sirve trafico critico de produccion como MLflow — si una iteracion sale mal, redeploys nuestros sin riesgo de compliance.

```hcl
resource "aws_ecr_repository" "mlflow" {
  name                 = "${var.project}-mlflow"
  image_tag_mutability = "IMMUTABLE" # v3.12.0 nunca cambia
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

resource "aws_ecr_repository" "reports" {
  name                 = "${var.project}-reports"
  image_tag_mutability = "MUTABLE" # iteramos nginx.conf seguido
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}
```

> **Checkpoint despues de pegar los 5 bloques**: ejecuta
> `terraform fmt infra/modules/storage/main.tf` para confirmar que el
> archivo es sintacticamente valido. Si reformatea, OK; si imprime
> error de parse, falta pegar un `}` o uniste dos resources sin
> separador.

### 3.4.3 `modules/storage/outputs.tf`

```hcl
output "data_bucket" { value = aws_s3_bucket.data.bucket }
output "data_bucket_arn" { value = aws_s3_bucket.data.arn }
output "artifacts_bucket" { value = aws_s3_bucket.artifacts.bucket }
output "artifacts_bucket_arn" { value = aws_s3_bucket.artifacts.arn }

output "ecr_trainer_url" { value = aws_ecr_repository.trainer.repository_url }
output "ecr_trainer_arn" { value = aws_ecr_repository.trainer.arn }
output "ecr_mlflow_url" { value = aws_ecr_repository.mlflow.repository_url }
output "ecr_mlflow_arn" { value = aws_ecr_repository.mlflow.arn }
output "ecr_reports_url" { value = aws_ecr_repository.reports.repository_url }
output "ecr_reports_arn" { value = aws_ecr_repository.reports.arn }
```

> **En consola AWS veras**:
> - S3 → Buckets → `ml-training-data-<suffix>` (vacio; Excel se sube
>   en Ola A) y `ml-training-artifacts-<suffix>` (artifacts + reports
>   + MLflow artifact store). Ambos con Versioning=Enabled, Encryption
>   AES256, Block public access ON.
> - S3 → Bucket `ml-training-artifacts-...` → Management → Lifecycle
>   rule → "expira versiones non-current a los 90 dias".
> - ECR → Repositories → 3: `ml-training`, `ml-training-mlflow`,
>   `ml-training-reports` (vacios hasta Ola B). Cada uno con scan-on-push
>   y lifecycle policy (keep last 10 tags + borrar untagged >7 dias).

### 3.4.4 Apendear `module "storage"` en `infra/envs/prod/main.tf`

Mismo patron: pegar AL FINAL de `infra/envs/prod/main.tf`
(despues del bloque `module "network"` de §3.3.4):

```hcl
# -------------------------------------------------------------------------
# Capa 2: Storage (S3 buckets + ECR repos)
# -------------------------------------------------------------------------
module "storage" {
  source  = "../../modules/storage"
  project = var.project
}
```

> **Checkpoint**: `terraform fmt && terraform validate` debe pasar.
> Storage es independiente de network (no comparte inputs) — por eso
> en Parte 4 hay una "Ola A" que aplica storage **solo**, antes que
> todo lo demas (§4.2). Asi tenes ECR listo para hacer `docker push`
> antes de levantar ECS.

---

## 3.4.5 `modules/_shared/` — Trust policies compartidos

Documentos de assume-role JSON que varios modulos repetian. Cada modulo los
carga con `file()` o `templatefile()` en vez de redeclarar el mismo `data
"aws_iam_policy_document"`. Cambio puramente de organizacion: AWS provider
normaliza JSON, asi que `terraform plan` queda no-op.

Archivos:
- `assume-ecs-tasks.json`      — Fargate / ECS task roles (mlflow, reports, batch job role)
- `assume-lambda.json`         — Lambda execution roles (dispatcher, notifier, scheduler)
- `assume-ec2.json`            — EC2 instance profile (batch compute env)
- `assume-batch-service.json`  — AWS Batch service role
- `assume-github-oidc.json.tftpl` — GHA OIDC trust (cicd + consumer-iam), parametriza provider_arn/org/repo
- `README.md`                  — documentacion inline del directorio

Patron de uso desde un modulo:

```hcl
resource "aws_iam_role" "ejemplo" {
  name               = "${var.project}-ejemplo"
  assume_role_policy = file("${path.module}/../_shared/assume-ecs-tasks.json")
}
```

Para el trust GHA-OIDC (con variables interpoladas):

```hcl
locals {
  gha_oidc_trust = templatefile("${path.module}/../_shared/assume-github-oidc.json.tftpl", {
    provider_arn = var.oidc_provider_arn
    org          = var.github_org
    repo         = var.github_repo
  })
}
```

Si necesitas un nuevo trust (ej. RDS, EventBridge), agregarlo aqui en vez de
inlinearlo en el modulo.

### 3.4.5.1 Contenido de los archivos

`modules/_shared/assume-ecs-tasks.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Principal": {
        "Service": "ecs-tasks.amazonaws.com"
      }
    }
  ]
}
```

`modules/_shared/assume-lambda.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      }
    }
  ]
}
```

`modules/_shared/assume-ec2.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Principal": {
        "Service": "ec2.amazonaws.com"
      }
    }
  ]
}
```

`modules/_shared/assume-batch-service.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Principal": {
        "Service": "batch.amazonaws.com"
      }
    }
  ]
}
```

`modules/_shared/assume-github-oidc.json.tftpl` (template — `${provider_arn}`,
`${org}` y `${repo}` se interpolan via `templatefile()`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Principal": {
        "Federated": "${provider_arn}"
      },
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:${org}/${repo}:*"
        }
      }
    }
  ]
}
```

---

## 3.5 `modules/mlflow/` — RDS + ECS Fargate + ALB

Este modulo es el mas pesado: arma el backend de tracking (RDS Postgres),
el server MLflow en Fargate y el ALB que expone todo. Aca lockean dos
contratos criticos del codigo del trainer:

- El argumento `--allowed-hosts '*'` (wildcard) evita el bug #11.2 de
  V1 (403 "Invalid Host header"). Es **wildcard intencional** porque
  el ALB DNS no se conoce en tiempo de `terraform plan`. Refinable
  post-stand-up: una vez que tenes el ALB DNS, podrias pasar a lista
  especifica `--allowed-hosts <alb-dns>,mlflow.local,localhost` (ver
  §10 hardening).
- El usuario Postgres se llama `mlflow` y la DB se llama `mlflow`
  (igual que en docker-compose local, para que el trainer no tenga que
  cambiar la connection string entre local y prod).

### 3.5.1 `modules/mlflow/variables.tf`

```hcl
variable "project" { type = string }
variable "vpc_id" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "private_subnet_ids" { type = list(string) }
variable "sg_alb_id" { type = string }
variable "sg_mlflow_id" { type = string }
variable "sg_rds_id" { type = string }
variable "rds_instance_class" { type = string }
variable "rds_allocated_storage_gb" {
  type    = number
  default = 20
}
variable "mlflow_image" { type = string }
variable "artifacts_bucket" { type = string }
variable "artifacts_bucket_arn" { type = string }
variable "log_retention_days" { type = number }
```

### 3.5.2 `modules/mlflow/main.tf`

Este `main.tf` es el archivo mas grande de la guia (~270 lineas). Lo
partimos en 5 sub-bloques para que puedas pegar uno, releer el "por
que", y pasar al siguiente. **Todos van al mismo archivo
`modules/mlflow/main.tf`** en este orden.

#### 3.5.2.a — RDS Postgres (password + subnet group + instance)

`random_password` + Secrets Manager evita escribir el password en
tfstate (queda solo en SM). `subnet_group` en private subnets x2
porque RDS exige 2 AZs aunque sea single-AZ. `skip_final_snapshot=true`
+ `deletion_protection=false` son **inseguros para prod con datos
reales**: nos los dejamos asi mientras hay solo experimentos
descartables. Al primer modelo que importe en Production, ambos
flags **deben cambiarse** (lo trata §10.4).

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `random_password.rds` | **(local Terraform)** | No es un recurso AWS — Terraform genera un string aleatorio en memoria. En Console serias VOS quien tipea un password en el wizard de RDS. |
> | `aws_secretsmanager_secret` + `_version` | **Secrets Manager** | `Secrets Manager > Secrets > Store a new secret > Other type`. **Key/value**: pega el password. **Name**: `ml-training-rds-password`. Encryption: `aws/secretsmanager` (default). |
> | `aws_db_subnet_group.mlflow` | **RDS** | `RDS > Subnet groups > Create DB subnet group`. **Name**: `ml-training-rds-subnets`. **VPC**: la tuya. **AZs**: las 2 que tenes. **Subnets**: las 2 private. |
> | `aws_db_instance.mlflow` | **RDS** | `RDS > Databases > Create database`. **Standard create > PostgreSQL > 15.x**. **Templates**: Production (o Dev/Test si querés single-AZ). **DB identifier**: `ml-training-mlflow`. **Master username**: `mlflow`. **Master password**: pega el secret. **Instance class**: `db.t3.micro`. **Storage**: 20 GB gp3. **Connectivity > VPC**: la tuya. **Subnet group**: el creado arriba. **VPC SGs**: `sg-rds`. **Public access**: No. **Backup retention**: 7 days. |
>
> **Conceptualmente**:
> - **RDS** es Postgres **managed por AWS** — AWS se encarga de backups automaticos, parches del SO, replicacion. Vos solo te conectas a la endpoint que te da (`ml-training-mlflow.XXXXX.us-east-1.rds.amazonaws.com:5432`).
> - **Por que Postgres aca**: MLflow lo usa como **backend store** — guarda metadata de runs (params, metrics, tags, experimento, run_id). Los **artifacts pesados** (modelos `.joblib`, dashboards `.html`) NO van a Postgres, van a S3 (separacion clave para que la DB no crezca a TB).
> - **Por que Secrets Manager y no env var**: el password queda **rotable** (podes rotar con `aws secretsmanager rotate-secret` sin re-deploy de Fargate — el container lee la version actual al arrancar). Ademas no aparece en `terraform.tfstate` en plano (solo el ARN del secret).
> - **`skip_final_snapshot=true` + `deletion_protection=false`**: cuando vos haces `terraform destroy`, RDS por default crea un snapshot final (te previene de perder data sin querer) y rechaza el destroy si deletion_protection=true. Los dos flags estan en "permisivo" para que la guia pueda destruir el lab sin friccion. **Cambialos a `false`/`true` antes de meter datos reales** (Parte 10.4).

```hcl
data "aws_region" "current" {}

resource "random_password" "rds" {
  length  = 32
  special = false # algunos chars rompen connection strings -> evitar
}

resource "aws_secretsmanager_secret" "rds" {
  name = "${var.project}-rds-password"
}

resource "aws_secretsmanager_secret_version" "rds" {
  secret_id     = aws_secretsmanager_secret.rds.id
  secret_string = random_password.rds.result
}

resource "aws_db_subnet_group" "mlflow" {
  name       = "${var.project}-rds-subnets"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_instance" "mlflow" {
  identifier              = "${var.project}-mlflow"
  engine                  = "postgres"
  engine_version          = "15"
  instance_class          = var.rds_instance_class
  allocated_storage       = var.rds_allocated_storage_gb
  storage_type            = "gp3"
  storage_encrypted       = true
  db_name                 = "mlflow"
  username                = "mlflow"
  password                = random_password.rds.result
  db_subnet_group_name    = aws_db_subnet_group.mlflow.name
  vpc_security_group_ids  = [var.sg_rds_id]
  publicly_accessible     = false
  skip_final_snapshot     = true # OK para prod single-AZ; ajustar en Parte 10.4
  apply_immediately       = true
  deletion_protection     = false # cambiar a true cuando hay datos productivos
  backup_retention_period = 7
  backup_window           = "06:00-07:00"
  maintenance_window      = "Mon:07:00-Mon:08:00"

  tags = { Name = "${var.project}-mlflow" }
}
```

#### 3.5.2.b — ALB (load balancer + target group + listener)

Un solo ALB sirve MLflow y reports — el listener default va a MLflow,
reports agrega una `listener_rule` desde §3.6. `idle_timeout=60` es
suficiente para ML training UI; subir si subis dashboards pesados.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_lb.main` | **EC2 > Load Balancers** | `Create Load Balancer > Application Load Balancer`. **Name**: `ml-training-alb`. **Scheme**: Internet-facing. **VPC**: la tuya. **Mappings**: ambas public subnets. **SGs**: `sg-alb`. **Listener**: HTTP :80 (HTTPS lo agregamos en Parte 10.1). |
> | `aws_lb_target_group.mlflow` | **EC2 > Target Groups** | `Create target group > IP addresses` (no Instances — Fargate usa IPs, no EC2 IDs). **Name**: `ml-training-tg-mlflow`. **Protocol/port**: HTTP/5000. **VPC**: la tuya. **Health check**: HTTP path `/health`, port 5000, healthy=2, unhealthy=5. |
> | `aws_lb_listener.http` | **EC2 > Load Balancers > [tu ALB] > Listeners** | `Add listener`. **Protocol/port**: HTTP/80. **Default action**: Forward to → target group `ml-training-tg-mlflow`. |
>
> **Conceptualmente**:
> - **ALB** = "el portero publico" — recibe todo trafico HTTP desde Internet en el puerto 80 y lo enruta a algun target group basado en reglas (path, host header, query string).
> - **Target Group** = lista de IPs/instances que pueden recibir trafico. Cada uno tiene un **health check** propio — el ALB pingea `/health` cada 30s; si 5 fallan seguidos, marca al target como unhealthy y deja de mandarle trafico. Por eso al hacer scale-up tenes que esperar ~3 min: el ALB recien empieza a mandar trafico cuando el health check pasa 2 veces.
> - **Listener** = "que hacer con el trafico de un puerto". El listener default forwarea TODO al TG de MLflow. En Parte 3.6 vamos a agregarle **listener rules** que digan "si el path matchea `/reports/*`, forward a otro TG (reports)". Asi UN solo ALB sirve dos services distintos (ahorra $16/mes vs tener 2 ALBs).
> - **Por que un solo ALB**: ALB cuesta ~$16/mes base + trafico. Con uno solo y reglas por path, podes servir varios services. Multi-ALB tiene sentido si querés aislamiento total (ej. ALB privado para internal + ALB publico para externo).

```hcl
resource "aws_lb" "main" {
  name               = "${var.project}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [var.sg_alb_id]
  subnets            = var.public_subnet_ids
  idle_timeout       = 60
}

# Default target group (MLflow). Reports agrega su propio TG en modulo
# reports y se asocia al listener via rule.
resource "aws_lb_target_group" "mlflow" {
  name        = "${var.project}-tg-mlflow"
  port        = 5000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  health_check {
    enabled             = true
    path                = "/health"
    port                = "5000"
    matcher             = "200"
    interval            = 30
    timeout             = 10
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }

  deregistration_delay = 30
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = "80"
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.mlflow.arn
  }
}
```

#### 3.5.2.c — ECS cluster + Service Discovery

Cluster compartido por MLflow y reports (un cluster, dos services).
`containerInsights=disabled` ahorra ~$2/mes (logs custom-metricas a
CloudWatch); activar si necesitas tracing detallado. Service Discovery
publica `mlflow.local:5000` internamente para que el trainer en Batch
no tenga que conocer el ALB DNS.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_ecs_cluster.main` | **ECS** | `ECS > Clusters > Create cluster`. **Name**: `ml-training-cluster`. **Infrastructure**: AWS Fargate (no EC2). **Monitoring > Container Insights**: disabled (Off). |
> | `aws_service_discovery_private_dns_namespace.main` | **Cloud Map** | `AWS Cloud Map > Namespaces > Create namespace`. **Name**: `ml-training.local`. **Type**: API calls and DNS queries in VPC. **VPC**: la tuya. |
> | `aws_service_discovery_service.mlflow` | **Cloud Map** | Dentro del namespace: `Create service`. **Name**: `mlflow`. **DNS records**: A record, TTL 10s. **Routing policy**: MULTIVALUE. |
>
> **Conceptualmente**:
> - **ECS Cluster** = container de logica para agrupar services + tasks. Aunque sea Fargate (no hay EC2 fisicas), seguis necesitando un "cluster" como entidad organizadora. Es gratis — no pagas por el cluster, pagas por las tasks que corren adentro.
> - **Cloud Map / Service Discovery** = un DNS interno automatico para tu VPC. Cuando crees el service `mlflow` mas abajo (3.5.2.e), Fargate va a registrar **automaticamente** la IP de cada task que arranca en este DNS. Asi otro container puede resolver `mlflow.ml-training.local` y obtiene la IP actual sin importar cuantas veces se haya re-deployado el service.
> - **Por que no usar el ALB DNS directamente**: para clientes EXTERNOS (browser, GitHub Actions) usamos ALB. Para clientes INTERNOS dentro de la VPC (el trainer en Batch que postea runs a MLflow), usamos Cloud Map → conexion directa task-to-task sin pasar por el ALB, mas barato (no carga el ALB) y mas rapido (no hace el roundtrip por public IPs).

```hcl
resource "aws_ecs_cluster" "main" {
  name = "${var.project}-cluster"
  setting {
    name  = "containerInsights"
    value = "disabled" # ahorra ~$2/mes; activar si necesitas tracing detallado
  }
}

# Service discovery namespace para que reports/batch resuelvan "mlflow.local"
resource "aws_service_discovery_private_dns_namespace" "main" {
  name        = "${var.project}.local"
  description = "Service discovery interno"
  vpc         = var.vpc_id
}

resource "aws_service_discovery_service" "mlflow" {
  name = "mlflow"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.main.id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }
  health_check_custom_config { failure_threshold = 1 }
}
```

#### 3.5.2.d — IAM (execution role + task role)

ECS Fargate distingue dos roles:
- **exec role**: lo asume el agente Fargate ANTES del container —
  permite pullear de ECR, escribir logs, leer secrets.
- **task role**: lo asume el container — permite acceso a S3
  artifacts. Por que separados: si el container es comprometido, el
  atacante solo obtiene los perms del task role (no ECR/Secrets).

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_iam_role.mlflow_exec` + attachment + inline policy | **IAM** | `IAM > Roles > Create role`. **Trusted entity**: AWS service > **Elastic Container Service Task**. **Permissions**: `AmazonECSTaskExecutionRolePolicy` (managed) + inline policy custom para `secretsmanager:GetSecretValue` sobre el secret RDS. **Role name**: `ml-training-mlflow-exec`. |
> | `aws_iam_role.mlflow_task` + inline policy | **IAM** | Mismo wizard. **Role name**: `ml-training-mlflow-task`. **Permissions**: inline policy con `s3:GetObject/PutObject/DeleteObject/ListBucket` sobre el bucket `ml-training-artifacts-*`. |
>
> **Conceptualmente — la separacion exec role vs task role es defense-in-depth**:
> - **Exec role** lo usa el "agente Fargate" (el daemon de AWS que arranca tu container). Hace cosas ANTES de que tu codigo corra: `docker pull` de ECR, `kms:Decrypt` del Secret, push de logs a CloudWatch. El codigo de MLflow nunca obtiene esas credenciales.
> - **Task role** lo usa **tu container** una vez corriendo. AWS lo inyecta como creds temporales accesibles via metadata endpoint (`http://169.254.170.2/v2/credentials/...`). Si alguien hace `boto3.client('s3')` adentro del container, esas creds son las del task role.
> - **El ataque que esto bloquea**: imagina que MLflow tiene un RCE y un atacante ejecuta codigo en el container. **Solo** obtiene los permisos del task role (read/write S3 artifacts). **NO** puede leer secrets de Secrets Manager ni pullear de ECR (esos son del exec role, fuera del container).
> - **Trust policy con `ecs-tasks.amazonaws.com`**: dice "solo el servicio ECS Fargate puede asumir este rol" (no usuarios IAM, no otras services). Otro tipo de defensa.

```hcl
# modules/mlflow/iam.tf
resource "aws_iam_role" "mlflow_exec" {
  name               = "${var.project}-mlflow-exec"
  assume_role_policy = file("${path.module}/../_shared/assume-ecs-tasks.json")
}

resource "aws_iam_role_policy_attachment" "mlflow_exec" {
  role       = aws_iam_role.mlflow_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Permitir leer el secret del RDS password
resource "aws_iam_role_policy" "mlflow_exec_secret" {
  role = aws_iam_role.mlflow_exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = aws_secretsmanager_secret.rds.arn
    }]
  })
}

resource "aws_iam_role" "mlflow_task" {
  name               = "${var.project}-mlflow-task"
  assume_role_policy = file("${path.module}/../_shared/assume-ecs-tasks.json")
}

resource "aws_iam_role_policy" "mlflow_task_s3" {
  role = aws_iam_role.mlflow_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
        "s3:ListBucket"
      ]
      Resource = [
        var.artifacts_bucket_arn,
        "${var.artifacts_bucket_arn}/*"
      ]
    }]
  })
}
```

> **Nota — assume policy centralizada**: los trust policies (`ecs-tasks`,
> `ec2`, `lambda`, `batch-service`, GHA-OIDC) viven como JSON estatico en
> `infra/modules/_shared/`. Cada modulo solo hace `file("${path.module}/../_shared/<archivo>.json")`.
> Antes el mismo `data "aws_iam_policy_document" "ecs_tasks_assume"` se
> redeclaraba copy-paste en mlflow, reports y batch — ahora una sola
> fuente, sin drift.

#### 3.5.2.e — Log group + Task Definition + Service

El task-def encapsula la receta del container (imagen, comando,
healthcheck, secrets). El service mantiene N replicas corriendo
(`desiredCount=1`) y se conecta al ALB target group. `ignore_changes
= [desired_count]` permite al scheduler bajar a 0 sin que el siguiente
`terraform apply` lo vuelva a subir.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_cloudwatch_log_group.mlflow` | **CloudWatch** | `CloudWatch > Log groups > Create log group`. **Name**: `/ecs/ml-training/mlflow`. **Retention**: 30 days (var.log_retention_days). |
> | `aws_ecs_task_definition.mlflow` | **ECS** | `ECS > Task definitions > Create new task definition (JSON)`. **Family**: `ml-training-mlflow`. **Launch type**: Fargate. **OS/Arch**: Linux/x86_64. **CPU/Memory**: 2 vCPU / 4 GB. **Task role**: el `mlflow_task` que creaste. **Task exec role**: el `mlflow_exec`. **Container**: name `mlflow`, image `<ecr-url>:v3.12.0`, port 5000, command `mlflow server ...`, env vars + secret `RDS_PASSWORD` desde Secrets Manager, log config awslogs, healthcheck `curl /health`. |
> | `aws_ecs_service.mlflow` | **ECS** | `Cluster > Create service`. **Launch type**: Fargate. **Task definition**: el de arriba (latest revision). **Service name**: `mlflow`. **Desired tasks**: 1. **Networking > VPC**: la tuya, **subnets**: private, **SG**: `sg-mlflow`, **Public IP**: Disabled. **Load balancing**: enable, target group: `ml-training-tg-mlflow`. **Service discovery**: enable, namespace `ml-training.local`, service `mlflow`. |
>
> **Conceptualmente — la trinidad ECS**:
> - **Log group**: contenedor en CloudWatch para los stdout/stderr del container. Cada task escribe un "log stream" (`mlflow/mlflow/<task-id>`) que vivira 30 dias. Util para debug post-mortem.
> - **Task definition**: la "receta" — describe COMO se debe correr un container (imagen, recursos, network, env). Es **inmutable**: cada cambio crea una "revision" nueva (`:1`, `:2`, ...). Si pifias algo, podes hacer rollback apuntando el service a una revision anterior.
> - **Service**: el "manager" — mantiene N tasks corriendo segun el task-def especificado. Si una task crashea, lo detecta y lanza otra (self-healing). Si actualizas a una revision nueva del task-def, hace un **rolling deployment**: arranca la nueva, espera a que pase healthcheck, recien ahi mata la vieja.
> - **`ignore_changes = [desired_count]`**: clave. El scheduler (Parte 3.10) cambia `desiredCount=0` para apagar de noche y `=1` para encender. Sin este `ignore_changes`, el proximo `terraform apply` veria "esta en 0, deberia ser 1" y lo volveria a encender, deshaciendo el scheduler.

```hcl
resource "aws_cloudwatch_log_group" "mlflow" {
  name              = "/ecs/${var.project}/mlflow"
  retention_in_days = var.log_retention_days
}

# Task definition
resource "aws_ecs_task_definition" "mlflow" {
  family                   = "${var.project}-mlflow"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "2048" # 2 vCPU
  memory                   = "4096" # 4 GB
  execution_role_arn       = aws_iam_role.mlflow_exec.arn
  task_role_arn            = aws_iam_role.mlflow_task.arn

  container_definitions = jsonencode([
    {
      name         = "mlflow"
      image        = var.mlflow_image
      essential    = true
      portMappings = [{ containerPort = 5000, protocol = "tcp" }]
      command = [
        "sh", "-c",
        join(" ", [
          "mlflow server",
          "--host 0.0.0.0 --port 5000",
          # Allowed-hosts wildcard: MLflow 3.x rechaza con 403 si el
          # Host: header no coincide. ALB DNS no se conoce en plan-time;
          # wildcard es la opcion mas simple. Hardening en §10.
          "--allowed-hosts '*'",
          "--backend-store-uri postgresql://mlflow:$$RDS_PASSWORD@${aws_db_instance.mlflow.address}:5432/mlflow",
          "--default-artifact-root s3://${var.artifacts_bucket}/artifacts",
          "--serve-artifacts"
        ])
      ]
      secrets = [{
        name      = "RDS_PASSWORD"
        valueFrom = aws_secretsmanager_secret.rds.arn
      }]
      environment = [
        { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.mlflow.name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "mlflow"
        }
      }
      healthCheck = {
        command     = ["CMD-SHELL", "python -c 'import urllib.request,sys; sys.exit(0 if urllib.request.urlopen(\"http://localhost:5000/health\",timeout=3).status==200 else 1)'"]
        interval    = 30
        timeout     = 5
        retries     = 5
        startPeriod = 60
      }
    }
  ])
}

resource "aws_ecs_service" "mlflow" {
  name            = "mlflow"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.mlflow.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  propagate_tags  = "SERVICE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.sg_mlflow_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.mlflow.arn
    container_name   = "mlflow"
    container_port   = 5000
  }

  service_registries {
    registry_arn = aws_service_discovery_service.mlflow.arn
  }

  # Ignore desired_count para que el scheduler lo pueda manejar sin drift
  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [aws_lb_listener.http]
}
```

> **Checkpoint despues de pegar 3.5.2.a-e**: `terraform fmt
> infra/modules/mlflow/main.tf` para verificar parse. Si reformatea
> sin errores, los 5 bloques quedaron concatenados correctamente.

### 3.5.3 `modules/mlflow/outputs.tf`

```hcl
output "tracking_uri" { value = "http://${aws_lb.main.dns_name}" }
output "alb_dns" { value = aws_lb.main.dns_name }
output "alb_arn" { value = aws_lb.main.arn }
output "alb_arn_suffix" { value = aws_lb.main.arn_suffix } # para CloudWatch dimensions
output "alb_listener_arn" { value = aws_lb_listener.http.arn }
output "cluster_id" { value = aws_ecs_cluster.main.id }
output "cluster_name" { value = aws_ecs_cluster.main.name }
output "service_name" { value = aws_ecs_service.mlflow.name }
output "rds_instance_id" { value = aws_db_instance.mlflow.id }
output "namespace_id" { value = aws_service_discovery_private_dns_namespace.main.id }
```

> **En consola AWS veras**:
> - RDS → Databases → `ml-training-mlflow` (engine=postgres15.4,
>   db.t3.micro, 20GB gp3, Single-AZ, Status=Available). **Es la unica
>   pieza con storage persistente del Model Registry** — apagarla con
>   `task aws:sleep` no borra data, solo deja de cobrar compute.
> - EC2 → Load Balancers → `ml-training-alb` (internet-facing). DNS
>   `ml-training-alb-XXXX.us-east-1.elb.amazonaws.com` — este es el
>   `MLFLOW_ALB_DNS` que va a las GitHub vars.
> - EC2 → Target Groups → `ml-training-mlflow-tg` (health=200 en `/`).
> - ECS → Clusters → `ml-training-cluster` → Services → `mlflow`
>   (desiredCount=1, runningCount=1, healthy).
> - ECS → Task Definitions → `ml-training-mlflow:N` (imagen custom de
>   ECR, env vars MLFLOW_BACKEND_STORE_URI + ARTIFACT_STORE).
> - Cloud Map (Service Discovery) → Namespaces → `<project>.local`
>   (interno, para que reports/batch resuelvan `mlflow.local:5000`).
> - Secrets Manager → `ml-training-rds-password` (con KMS aws/secretsmanager).
> - CloudWatch → Log groups → `/ecs/ml-training/mlflow` (logs Fargate).

### 3.5.4 Apendear `module "mlflow"` en `infra/envs/prod/main.tf`

Pegar AL FINAL de `infra/envs/prod/main.tf` (despues de
`module "storage"` de §3.4.4):

```hcl
# -------------------------------------------------------------------------
# Capa 3: MLflow (RDS + ECS Fargate + ALB)
# -------------------------------------------------------------------------
module "mlflow" {
  source = "../../modules/mlflow"

  project              = var.project
  vpc_id               = module.network.vpc_id
  public_subnet_ids    = module.network.public_subnet_ids
  private_subnet_ids   = module.network.private_subnet_ids
  sg_alb_id            = module.network.sg_alb_id
  sg_mlflow_id         = module.network.sg_mlflow_id
  sg_rds_id            = module.network.sg_rds_id
  rds_instance_class   = var.rds_instance_class
  mlflow_image         = "${module.storage.ecr_mlflow_url}:${var.mlflow_image_tag}"
  artifacts_bucket     = module.storage.artifacts_bucket
  artifacts_bucket_arn = module.storage.artifacts_bucket_arn
  log_retention_days   = var.log_retention_days
}
```

> **Checkpoint**: este es el **primer modulo con dependencias** —
> consume outputs de `module.network` (VPC, subnets, SGs) y
> `module.storage` (ECR url, bucket de artifacts). Si `terraform
> validate` falla con "Unsupported attribute" o "Reference to
> undeclared module", revisa que pegaste §3.3.4 y §3.4.4 antes que
> esto.

---

## 3.6 `modules/reports/` — Fargate nginx sirviendo S3

Sirve `s3://artifacts/{reports,artifacts}/` como sitio estatico bajo
`http://<ALB>/reports/*` y `http://<ALB>/artifacts/*`. Usa el mismo ALB
listener via `path-pattern` rules.

Mecanismo: container = nginx + sidecar de `aws s3 sync` que copia el
bucket a `/usr/share/nginx/html/` cada 60s. Costo: $0.50/mes Fargate +
trafico S3 GET despreciable.

### 3.6.1 `modules/reports/variables.tf`

```hcl
variable "project" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "sg_mlflow_id" { type = string } # SG con ingress :80 desde sg-alb; reports lo reusa
variable "ecs_cluster_id" { type = string }
variable "alb_listener_arn" { type = string }
variable "artifacts_bucket" { type = string }
variable "artifacts_bucket_arn" { type = string }
variable "reports_image" { type = string }
variable "log_retention_days" { type = number }
```

> **Por que reusa `sg_mlflow_id` y no `sg_alb_id`**: el task de reports
> vive en private subnets y necesita ingress :80 desde el ALB (que esta
> en sg_alb). `sg_mlflow` justamente abre :80 desde sg_alb. Si le
> pasaramos `sg_alb_id` directamente, el task aceptaria :80 desde
> 0.0.0.0/0 (la regla de sg_alb), lo cual es peligroso y ademas no
> coincide con el ingress que el ALB realmente envia.

### 3.6.2 `modules/reports/main.tf`

> **Equivalente en AWS Console — vista general del modulo reports**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_lb_target_group.reports` | **EC2 > Target Groups** | `Create target group > IP addresses`. **Name**: `ml-training-tg-reports`. HTTP/80. Health check path: `/healthz`. |
> | `aws_lb_listener_rule.reports_path` | **EC2 > Load Balancers > [ALB] > Listeners > HTTP:80 > Manage rules** | `Insert rule`. **Priority**: 100 (menor = mas prioritario). **IF Path is `/reports/*` OR `/reports` OR `/artifacts/*` OR `/artifacts`** → **THEN Forward to** `ml-training-tg-reports`. Los 4 paths son necesarios: el `/*` matchea `/reports/foo.html` pero NO matchea el listado raw `/reports` (sin slash final); por eso se incluyen ambas variantes. Default action (forward a MLflow TG) queda como fallback. |
> | `aws_iam_role.reports_exec` + `reports_task` | **IAM** | Mismo wizard que MLflow exec/task roles, pero el task role tiene `s3:GetObject + ListBucket` solo (no PUT — reports es read-only sobre artifacts). |
> | `aws_cloudwatch_log_group.reports` | **CloudWatch** | `Create log group`. Name: `/ecs/ml-training/reports`. |
> | `aws_ecs_task_definition.reports` | **ECS > Task definitions** | Mismo wizard. CPU/Mem: 0.5 vCPU / 1 GB (es solo nginx). Image: `<ecr-url>/ml-training-reports:latest`. Env: `S3_BUCKET=<artifacts-bucket>`. |
> | `aws_ecs_service.reports` | **ECS > Cluster > Services** | Mismo wizard. **Cluster**: el `ml-training-cluster` ya existente (NO crear otro). **Service name**: `reports`. **Target group**: el de reports. |
>
> **Conceptualmente — el patron "Fargate sidecar de S3"**:
> - Reports es un **nginx sirviendo HTML estatico**. La data viene de S3 (dashboards generados por el trainer). Hay 3 maneras de hacer esto en AWS:
>   1. **CloudFront + S3 directo** (mas barato $0.50/mes, pero los dashboards deben ser publicos o requeris OAI/OAC config).
>   2. **API Gateway + Lambda + S3** (serverless, $0 fixed pero $$$$ por request).
>   3. **Fargate nginx + sidecar `aws s3 sync`** (este enfoque — $4-5/mes pero reusa el ALB existente y mantiene los reports privados detras del SG).
> - **El truco del modulo**: reusa el ALB (ahorra otro $16/mes), reusa el cluster ECS (ahorra cluster fees=$0 pero menos manejo), reusa el SG `sg_mlflow` (que ya tiene ingress :80 desde el ALB).
> - **Listener rules son ORDENADAS por priority**: el ALB las evalua de menor a mayor. La rule `priority=100` evalua ANTES del default action. Si pones priority=200 a otra rule, se evalua DESPUES de la 100. La default action es siempre la ultima.

```hcl
data "aws_region" "current" {}

# Target group
resource "aws_lb_target_group" "reports" {
  name        = "${var.project}-tg-reports"
  port        = 80
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  health_check {
    path                = "/healthz"
    interval            = 30
    timeout             = 10
    healthy_threshold   = 2
    unhealthy_threshold = 5
    matcher             = "200"
  }
}

# Listener rules: /reports/* y /artifacts/* -> reports TG
resource "aws_lb_listener_rule" "reports_path" {
  listener_arn = var.alb_listener_arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.reports.arn
  }
  condition {
    path_pattern { values = ["/reports/*", "/reports", "/artifacts/*", "/artifacts"] }
  }
}

# IAM (assume policy compartida en infra/modules/_shared/)
resource "aws_iam_role" "reports_exec" {
  name               = "${var.project}-reports-exec"
  assume_role_policy = file("${path.module}/../_shared/assume-ecs-tasks.json")
}

resource "aws_iam_role_policy_attachment" "reports_exec" {
  role       = aws_iam_role.reports_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "reports_task" {
  name               = "${var.project}-reports-task"
  assume_role_policy = file("${path.module}/../_shared/assume-ecs-tasks.json")
}

resource "aws_iam_role_policy" "reports_task_s3" {
  role = aws_iam_role.reports_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:ListBucket"]
      Resource = [var.artifacts_bucket_arn, "${var.artifacts_bucket_arn}/*"]
    }]
  })
}

resource "aws_cloudwatch_log_group" "reports" {
  name              = "/ecs/${var.project}/reports"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "reports" {
  family                   = "${var.project}-reports"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"  # 0.5 vCPU
  memory                   = "1024" # 1 GB
  execution_role_arn       = aws_iam_role.reports_exec.arn
  task_role_arn            = aws_iam_role.reports_task.arn

  container_definitions = jsonencode([
    {
      name         = "reports"
      image        = var.reports_image
      essential    = true
      portMappings = [{ containerPort = 80, protocol = "tcp" }]
      environment = [
        { name = "S3_BUCKET", value = var.artifacts_bucket },
        { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.reports.name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "reports"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "reports" {
  name            = "reports"
  cluster         = var.ecs_cluster_id
  task_definition = aws_ecs_task_definition.reports.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.sg_mlflow_id] # mismo SG que mlflow: ingress :80 desde sg-alb
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.reports.arn
    container_name   = "reports"
    container_port   = 80
  }

  lifecycle {
    ignore_changes = [desired_count]
  }
}
```

### 3.6.3 `modules/reports/outputs.tf`

```hcl
output "service_name" { value = aws_ecs_service.reports.name }
```

> **En consola AWS veras**:
> - EC2 → Load Balancers → `ml-training-alb` → Listeners → HTTP:80 →
>   Rules: 2 nuevas con `path-pattern=/reports/*` y `/artifacts/*` que
>   ruteo al target group `ml-training-reports-tg`. Default (/) sigue
>   yendo al de MLflow.
> - EC2 → Target Groups → `ml-training-reports-tg` (health=200 en
>   `/healthz`).
> - ECS → Cluster `ml-training-cluster` → Services → `reports` (segundo
>   service, mismo cluster que MLflow). Task definition con imagen
>   custom de ECR `ml-training-reports`.
> - CloudWatch → Log groups → `/ecs/ml-training/reports`.

### 3.6.4 `docker/reports/Dockerfile`

Imagen custom: nginx + `aws s3 sync` cada 60s en background.

```dockerfile
FROM nginx:1.27-alpine

RUN apk add --no-cache aws-cli bash dumb-init

# config nginx que sirve /usr/share/nginx/html con autoindex
COPY docker/reports/nginx.conf /etc/nginx/conf.d/default.conf
COPY docker/reports/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80
ENTRYPOINT ["/usr/bin/dumb-init", "--", "/entrypoint.sh"]
```

### 3.6.5 `docker/reports/nginx.conf`

```nginx
server {
  listen 80 default_server;
  server_name _;

  # Health check para ALB target group
  location = /healthz {
    access_log off;
    return 200 "ok\n";
    add_header Content-Type text/plain;
  }

  # /reports/* -> /usr/share/nginx/html/reports/*
  # /artifacts/* -> /usr/share/nginx/html/artifacts/*
  location / {
    root /usr/share/nginx/html;
    autoindex on;
    autoindex_exact_size off;
    autoindex_localtime on;
    add_header Cache-Control "no-store";
  }
}
```

### 3.6.6 `docker/reports/entrypoint.sh`

```bash
#!/bin/bash
set -e

: "${S3_BUCKET:?S3_BUCKET requerido}"
: "${AWS_DEFAULT_REGION:?AWS_DEFAULT_REGION requerido}"

mkdir -p /usr/share/nginx/html/reports /usr/share/nginx/html/artifacts

# Sync inicial (bloqueante: arrancamos nginx con data ya cargada)
aws s3 sync "s3://${S3_BUCKET}/reports/"   /usr/share/nginx/html/reports/   --no-progress || true
aws s3 sync "s3://${S3_BUCKET}/artifacts/" /usr/share/nginx/html/artifacts/ --no-progress || true

# Sync loop en background (cada 60s)
(
  while true; do
    sleep 60
    aws s3 sync "s3://${S3_BUCKET}/reports/"   /usr/share/nginx/html/reports/   --delete --no-progress >/dev/null 2>&1 || true
    aws s3 sync "s3://${S3_BUCKET}/artifacts/" /usr/share/nginx/html/artifacts/ --delete --no-progress >/dev/null 2>&1 || true
  done
) &

# Foreground: nginx
exec nginx -g 'daemon off;'
```

### 3.6.7 Apendear `module "reports"` en `infra/envs/prod/main.tf`

Pegar AL FINAL de `infra/envs/prod/main.tf` (despues de
`module "mlflow"` de §3.5.4):

```hcl
# -------------------------------------------------------------------------
# Capa 4: Reports (Fargate nginx, mismo cluster + ALB que MLflow)
# -------------------------------------------------------------------------
module "reports" {
  source = "../../modules/reports"

  project              = var.project
  vpc_id               = module.network.vpc_id
  private_subnet_ids   = module.network.private_subnet_ids
  sg_mlflow_id         = module.network.sg_mlflow_id
  ecs_cluster_id       = module.mlflow.cluster_id
  alb_listener_arn     = module.mlflow.alb_listener_arn
  artifacts_bucket     = module.storage.artifacts_bucket
  artifacts_bucket_arn = module.storage.artifacts_bucket_arn
  reports_image        = "${module.storage.ecr_reports_url}:${var.reports_image_tag}"
  log_retention_days   = var.log_retention_days
}
```

> **Checkpoint**: `reports` reusa el `cluster_id` y `alb_listener_arn`
> de `module.mlflow` — por eso §3.5.4 **tiene que estar antes**. Si
> intentas validar sin haber pegado mlflow, vas a ver "Reference to
> undeclared module module.mlflow".

---

## 3.7 `modules/batch/` — Compute envs + queues + job-def + IAM

Pieza critica donde se respeta el contrato del trainer: el container
recibe via CMD `--varieties X --tuning Y` (matchea
`src/orchestration/cli.py:parse_args`) y las env vars S3 que
`main.py:_hydrate_data_from_s3` lee.

### 3.7.1 `modules/batch/variables.tf`

```hcl
variable "project" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "sg_batch_id" { type = string }
variable "ecr_trainer_url" { type = string }
variable "trainer_image_tag" { type = string }

variable "spot_max_vcpus" { type = number }
variable "ondemand_max_vcpus" { type = number }
variable "spot_bid_percentage" {
  type    = number
  default = 70
}
variable "instance_type" { type = string }

variable "tracking_uri" { type = string }
variable "artifacts_bucket" { type = string }
variable "artifacts_bucket_arn" { type = string }
variable "data_bucket" { type = string }
variable "data_bucket_arn" { type = string }

variable "job_attempt_seconds" {
  type    = number
  default = 28800 # 8h hard ceiling (incluye prod_xl)
}

variable "log_retention_days" { type = number }
```

### 3.7.2 `modules/batch/iam.tf`

> **Equivalente en AWS Console — los 4 roles IAM del modulo batch**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_iam_role.batch_instance` + `aws_iam_instance_profile.batch` | **IAM** | `IAM > Roles > Create role`. **Trusted entity**: AWS service > **EC2**. **Permissions**: `AmazonEC2ContainerServiceforEC2Role` (managed). **Name**: `ml-training-batch-instance`. Despues `IAM > Instance Profiles` (deprecated en Console moderna — el wizard de role crea el instance profile automaticamente). |
> | `aws_iam_role.job` + 2 inline policies (S3 + CloudWatch) | **IAM** | `Create role`. **Trusted entity**: AWS service > **Elastic Container Service Task**. **Permissions**: 2 inline policies — (a) S3: Get/Put/List sobre buckets data y artifacts; (b) CloudWatch: PutMetricData. **Name**: `ml-training-job-role`. |
> | `aws_iam_role.exec` | **IAM** | Mismo wizard. **Permissions**: `AmazonECSTaskExecutionRolePolicy` (managed). **Name**: `ml-training-job-exec`. |
> | `aws_iam_role.batch_service` | **IAM** | `Create role`. **Trusted entity**: AWS service > **AWS Batch**. **Permissions**: `AWSBatchServiceRole` (managed). **Name**: `ml-training-batch-service`. |
>
> **Conceptualmente — por que CUATRO roles distintos**:
> - **instance** = lo asume **la EC2 fisica** que Batch arranca (cuando es Spot/On-Demand, no Fargate). Permite a la EC2 reportar al cluster ECS subyacente (Batch usa ECS bajo el capo).
> - **job (task role)** = lo asume **tu container** (el trainer). Aca van los permisos S3 read/write + CloudWatch PutMetric. Estos son los unicos permisos que tu codigo Python ve.
> - **exec** = lo asume **el agente Fargate/ECS** ANTES de tu container. Permite pullear de ECR, escribir logs en CloudWatch.
> - **batch_service** = lo asume **el servicio AWS Batch** para gestionar tus compute environments (crear/destruir EC2s, escalar, monitorear). No lo asumi vos ni tu codigo nunca.
> - **Por que tanta separacion**: cada rol tiene **el minimo permiso necesario**. Un atacante que comprometa el trainer (job role) NO puede destruir EC2s (eso es batch_service), NO puede modificar el cluster (eso es instance), y NO puede leer secrets (no estan en ningun rol del trainer). Es el principio "**least privilege**" llevado al extremo.

```hcl
# modules/batch/iam.tf
# Trust policies viven como JSON estatico en infra/modules/_shared/
# (assume-ec2.json, assume-ecs-tasks.json, assume-batch-service.json).

# Role asumido por la EC2 que lanza Batch (instance profile)
resource "aws_iam_role" "batch_instance" {
  name               = "${var.project}-batch-instance"
  assume_role_policy = file("${path.module}/../_shared/assume-ec2.json")
}

resource "aws_iam_role_policy_attachment" "batch_instance" {
  role       = aws_iam_role.batch_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "batch" {
  name = "${var.project}-batch-instance"
  role = aws_iam_role.batch_instance.name
}

# Role asumido por el container (task) durante el job
resource "aws_iam_role" "job" {
  name               = "${var.project}-job-role"
  assume_role_policy = file("${path.module}/../_shared/assume-ecs-tasks.json")
}

# S3: el trainer necesita:
#  - GetObject en s3://data/ (hydrate del Excel acumulado)
#  - PutObject en s3://artifacts/{artifacts,reports}/ (sync de outputs)
#  - PutObject en s3://artifacts/artifacts/ (MLflow log_artifact)
resource "aws_iam_role_policy" "job_s3" {
  role = aws_iam_role.job.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.data_bucket_arn, "${var.data_bucket_arn}/*"]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"
        ]
        Resource = [var.artifacts_bucket_arn, "${var.artifacts_bucket_arn}/*"]
      }
    ]
  })
}

# CloudWatch: para emitir custom metric MAPE desde el trainer (Parte 5)
resource "aws_iam_role_policy" "job_cloudwatch" {
  role = aws_iam_role.job.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["cloudwatch:PutMetricData"]
      Resource = "*"
    }]
  })
}

# Execution role (pull image, write logs) — usado por Batch para arrancar
resource "aws_iam_role" "exec" {
  name               = "${var.project}-job-exec"
  assume_role_policy = file("${path.module}/../_shared/assume-ecs-tasks.json")
}

resource "aws_iam_role_policy_attachment" "exec" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Service role de Batch (gestion de CE).
# Antes era inline `jsonencode({...})`; ahora consume el JSON shared.
resource "aws_iam_role" "batch_service" {
  name               = "${var.project}-batch-service"
  assume_role_policy = file("${path.module}/../_shared/assume-batch-service.json")
}

resource "aws_iam_role_policy_attachment" "batch_service" {
  role       = aws_iam_role.batch_service.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}
```

### 3.7.3 `modules/batch/main.tf`

4 sub-bloques (todos al mismo `modules/batch/main.tf`):

#### 3.7.3.a — Log group

Logs de TODOS los Batch jobs en un solo group. Retencion configurable
desde `envs/prod/terraform.tfvars` (default 14 dias).

```hcl
data "aws_region" "current" {}

resource "aws_cloudwatch_log_group" "batch" {
  name              = "/aws/batch/${var.project}"
  retention_in_days = var.log_retention_days
}
```

#### 3.7.3.b — Compute Environments (Spot + OnDemand)

2 CEs: Spot (default, ~70% mas barato, puede interrumpir) y OnDemand
(reservado para `prod_xl` que tarda 5-6h y no tolera Spot kills).
`min_vcpus=0` permite que Batch escale a 0 cuando no hay jobs (ahorro
total fuera de horario). `ignore_changes = desired_vcpus` evita
drift cuando Batch scaling lo cambia entre apply y apply.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_batch_compute_environment.spot` | **AWS Batch** | `Batch > Compute environments > Create`. **Type**: Managed. **Name**: `ml-training-ce-spot`. **Provisioning model**: **Spot** (importante). **Bid percentage**: 50 (paga max 50% del precio On-Demand). **Allocation strategy**: SPOT_CAPACITY_OPTIMIZED. **Min/Max vCPUs**: 0 / 16. **Instance types**: `c6i.2xlarge`. **VPC**: la tuya, **Subnets**: las private, **SG**: `sg-batch`. **Instance role**: `ml-training-batch-instance`. **Service role**: `ml-training-batch-service`. |
> | `aws_batch_compute_environment.ondemand` | **AWS Batch** | Mismo wizard pero **Provisioning model**: **On-Demand** (EC2). **Allocation strategy**: BEST_FIT_PROGRESSIVE. Resto igual. |
>
> **Conceptualmente — Compute Environment = "pool autoscaleable de EC2s"**:
> - Cuando submitis un job, Batch mira la queue → consulta su CE asociado → si no hay EC2s con capacidad, **arranca una EC2 nueva** del tipo configurado. Cuando termina el job y no hay mas trabajo, Batch **apaga las EC2s** (escala a `min_vcpus=0`). Asi pagas EC2 SOLO durante el tiempo del job (~$0.03/hora c6i.2xlarge Spot).
> - **Spot vs On-Demand**: 
>   - **Spot**: AWS te alquila EC2s "sobrantes" a 60-90% de descuento. La trampa: AWS puede **interrumpir** la EC2 con 2 min de aviso si necesita la capacidad. Batch detecta la interrupcion y re-encola el job (segun `retry_strategy`).
>   - **On-Demand**: precio full, pero AWS garantiza que NO te quita la EC2 hasta que termines.
>   - **Cuando usar cual**: jobs cortos (<30 min) → Spot (interrupciones raras a esa escala, ahorro x3). Jobs largos (>4h) o con HPO costoso que perderias todo → On-Demand. El dispatcher de Parte 3.9 elige automaticamente segun `tuning`.
> - **`min_vcpus=0`** = escala a CERO cuando no hay jobs. Sin esto, Batch mantendria EC2s prendidas en idle (cuesta $$$). Con `min_vcpus=0`, despues de 5 min sin jobs Batch apaga todo.
> - **`ignore_changes = desired_vcpus`**: Batch lo modifica internamente segun la carga, no tiene sentido que Terraform "lo arregle" en cada apply.

```hcl
resource "aws_batch_compute_environment" "spot" {
  # AWS provider v6+ usa `name`. El atributo `compute_environment_name`
  # fue deprecado en v5 y eliminado en v6 -> con `aws ~> 6.0` lockeado
  # en §3.2.1, `terraform validate` falla si se usa el nombre viejo.
  name         = "${var.project}-ce-spot"
  service_role = aws_iam_role.batch_service.arn
  type         = "MANAGED"
  state        = "ENABLED"

  compute_resources {
    type                = "SPOT"
    bid_percentage      = var.spot_bid_percentage
    allocation_strategy = "SPOT_CAPACITY_OPTIMIZED"
    min_vcpus           = 0
    max_vcpus           = var.spot_max_vcpus
    desired_vcpus       = 0
    instance_type       = [var.instance_type]
    subnets             = var.private_subnet_ids
    security_group_ids  = [var.sg_batch_id]
    instance_role       = aws_iam_instance_profile.batch.arn
    tags                = { Name = "${var.project}-batch-spot" }
  }

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [compute_resources[0].desired_vcpus]
  }
}

resource "aws_batch_compute_environment" "ondemand" {
  name         = "${var.project}-ce-ondemand"   # ver nota en bloque "spot" sobre v6
  service_role = aws_iam_role.batch_service.arn
  type         = "MANAGED"
  state        = "ENABLED"

  compute_resources {
    type                = "EC2"
    allocation_strategy = "BEST_FIT_PROGRESSIVE"
    min_vcpus           = 0
    max_vcpus           = var.ondemand_max_vcpus
    desired_vcpus       = 0
    instance_type       = [var.instance_type]
    subnets             = var.private_subnet_ids
    security_group_ids  = [var.sg_batch_id]
    instance_role       = aws_iam_instance_profile.batch.arn
    tags                = { Name = "${var.project}-batch-od" }
  }

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [compute_resources[0].desired_vcpus]
  }
}
```

#### 3.7.3.c — Job queues (1 por CE)

Una queue por CE. El Lambda dispatcher (§3.9.5) elige queue por
`tuning`: `prod_xl → ondemand`, resto → spot. Priority=1 en ambas
(no hay queueing entre ellas, son disjuntas).

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_batch_job_queue.spot` | **AWS Batch** | `Batch > Job queues > Create`. **Name**: `ml-training-job-queue-spot`. **State**: Enabled. **Priority**: 1. **Connected compute environments > Add**: `ml-training-ce-spot`, order 1. |
> | `aws_batch_job_queue.ondemand` | **AWS Batch** | Mismo wizard, name `-ondemand`, conectado a `ml-training-ce-ondemand`. |
>
> **Conceptualmente — Queue es donde "depositas" jobs**:
> - El usuario / Lambda dispatcher hace `aws batch submit-job --job-queue ...`. La queue acepta el job y lo deja en estado `SUBMITTED` → `PENDING` → `RUNNABLE`. Cuando hay capacidad en el CE asociado, pasa a `STARTING` → `RUNNING` → `SUCCEEDED`/`FAILED`.
> - **Por que 2 queues separadas y no una con 2 CEs**: AWS soporta queue con multiple CEs (en orden de prioridad: si CE-A esta lleno, intenta CE-B). PERO eso te da menos control: vos queres que `prod_xl` SIEMPRE vaya a OD (nunca Spot, jamas), y resto SIEMPRE Spot. Con 2 queues separadas, el dispatcher elige explicitamente y no hay riesgo de "spillover".

```hcl
resource "aws_batch_job_queue" "spot" {
  name     = "${var.project}-job-queue-spot"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.spot.arn
  }
}

resource "aws_batch_job_queue" "ondemand" {
  name     = "${var.project}-job-queue-ondemand"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.ondemand.arn
  }
}
```

#### 3.7.3.d — Job definition (contrato con el trainer)

El campo `command = ["--varieties","POP","--tuning","smoke"]` es default
— el dispatcher (§3.9.5) lo sobreescribe por job. `retry_strategy`
solo reintenta cuando AWS Spot mata el host (no en error del trainer).
`timeout = job_attempt_seconds` (default 28800 = 8h) corta jobs colgados.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_batch_job_definition.trainer` | **AWS Batch** | `Batch > Job definitions > Create`. **Type**: Single-node. **Platform type**: EC2 (no Fargate — necesitamos c6i.2xlarge). **Name**: `ml-training-trainer`. **Container properties**: **Image**: `<ecr-url>/ml-training:v0.1.0`, **vCPUs**: 8, **Memory**: 14000 MiB, **Command**: `["--varieties","POP","--tuning","smoke"]` (default; el dispatcher lo sobreescribe). **Job role**: `ml-training-job-role`. **Execution role**: `ml-training-job-exec`. **Network**: assignPublicIp Disabled. **Environment variables**: `MLFLOW_TRACKING_URI`, `S3_ARTIFACTS_BUCKET`, etc. **Log configuration**: awslogs, group `/aws/batch/ml-training`. **Timeout**: 28800 sec (8h). **Retry strategy**: attempts=2 con `evaluate_on_exit` para reintentar solo en interrupciones Spot. |
>
> **Conceptualmente — Job Definition = "receta inmutable de como correr el trainer"**:
> - Es una plantilla. Cuando submitis un job (`aws batch submit-job --job-definition ml-training-trainer`), Batch toma esta receta y lanza un container basado en ella. Podes sobreescribir campos por submit (ej. el dispatcher cambia `command` para meter `--varieties` distinto cada vez).
> - **Cada cambio crea una nueva REVISION** (`:1`, `:2`, ...). Si bumpas la imagen `v0.1.0 → v0.2.0`, queda revision `:2`. El siguiente submit usa la latest revision. Podes rollback apuntando a `:1` explicitamente.
> - **`retry_strategy` con `evaluate_on_exit`**: muy importante. Sin reglas, AWS retry-ea AUTOMATICAMENTE en cualquier fallo (incluido un bug del trainer) — gastas $$$ en jobs rotos. Con las reglas:
>   - `on_status_reason = "Host EC2*"` → Spot mato la EC2 → RETRY (otra EC2 nueva).
>   - `on_reason = "*"` → cualquier otro fallo (incluido exit code del trainer) → EXIT (no reintenta).
> - **`timeout = 28800`**: si un job corre mas de 8h, Batch lo mata. Cubre el caso "trainer colgado por bug" (no quieres pagar 24h de EC2).

```hcl
resource "aws_batch_job_definition" "trainer" {
  name = "${var.project}-trainer"
  type = "container"

  retry_strategy {
    attempts = 2
    # Auto-retry solo cuando Spot interrumpe el host (preserva exit codes
    # del trainer; un error real no se reintenta)
    evaluate_on_exit {
      action           = "RETRY"
      on_status_reason = "Host EC2*"
    }
    evaluate_on_exit {
      action    = "EXIT"
      on_reason = "*"
    }
  }

  timeout {
    attempt_duration_seconds = var.job_attempt_seconds
  }

  container_properties = jsonencode({
    image                = "${var.ecr_trainer_url}:${var.trainer_image_tag}"
    vcpus                = 8     # c6i.2xlarge tiene 8 vCPU
    memory               = 14000 # de los 16 GB, dejamos ~2 GB para kernel + Batch agent
    jobRoleArn           = aws_iam_role.job.arn
    executionRoleArn     = aws_iam_role.exec.arn
    networkConfiguration = { assignPublicIp = "DISABLED" }
    # Sobreescrito por Lambda dispatcher (Sec 3.9.5) en cada submit.
    command = ["--varieties", "POP", "--tuning", "smoke"]
    environment = [
      { name = "MLFLOW_TRACKING_URI", value = var.tracking_uri },
      { name = "S3_ARTIFACTS_BUCKET", value = var.artifacts_bucket },
      { name = "S3_ARTIFACTS_PREFIX", value = "artifacts" },
      { name = "S3_REPORTS_PREFIX", value = "reports" },
      # S3_DATA_BUCKET / S3_DATA_KEY se inyectan por job (varia por submit)
      { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name },
      { name = "PYTHONUNBUFFERED", value = "1" }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.batch.name
        awslogs-region        = data.aws_region.current.name
        awslogs-stream-prefix = "trainer"
      }
    }
  })

  propagate_tags = true
  tags = {
    Project = var.project
  }
}
```

> **Checkpoint despues de 3.7.3.a-d**: `terraform fmt
> infra/modules/batch/main.tf`.

### 3.7.4 `modules/batch/outputs.tf`

```hcl
output "job_queue_spot" { value = aws_batch_job_queue.spot.name }
output "job_queue_spot_arn" { value = aws_batch_job_queue.spot.arn }
output "job_queue_ondemand" { value = aws_batch_job_queue.ondemand.name }
output "job_queue_ondemand_arn" { value = aws_batch_job_queue.ondemand.arn }
output "job_definition_name" { value = aws_batch_job_definition.trainer.name }
output "job_definition_arn" { value = aws_batch_job_definition.trainer.arn }
output "log_group_name" { value = aws_cloudwatch_log_group.batch.name }
```

> **En consola AWS veras**:
> - Batch → Compute environments → 2: `ml-training-ce-spot` (instance
>   types m5.large/m5.xlarge/c5.large/c5.xlarge, Spot allocationStrategy
>   SPOT_CAPACITY_OPTIMIZED) y `ml-training-ce-ondemand` (mismas
>   instance types, EC2). Ambas con `state=ENABLED, status=VALID`.
> - Batch → Job queues → 2: `ml-training-job-queue-spot` (priority=1,
>   conecta a ce-spot) y `-ondemand` (priority=2). Ambas `VALID`.
> - Batch → Job definitions → `ml-training-trainer` (revision N, type
>   container, imagen del ECR `ml-training:latest`). Es lo que el Lambda
>   dispatcher (§3.9) invoca con SubmitJob.
> - IAM → Roles → `ml-training-batch-instance-role` (EC2 lanzadora),
>   `ml-training-batch-job-role` (lo asume el container del trainer:
>   S3 r/w + CloudWatch PutMetricData), `ml-training-batch-exec-role`
>   (ECR pull + logs write), `ml-training-batch-service-role`
>   (gestion del CE).
> - CloudWatch → Log groups → `/aws/batch/ml-training` con
>   `retention=14 days`.

### 3.7.5 Apendear `module "batch"` en `infra/envs/prod/main.tf`

Pegar AL FINAL de `infra/envs/prod/main.tf` (despues de
`module "reports"` de §3.6.7):

```hcl
# -------------------------------------------------------------------------
# Capa 5: Batch (Spot + OD queues, job-def, IAM)
# -------------------------------------------------------------------------
module "batch" {
  source = "../../modules/batch"

  project              = var.project
  private_subnet_ids   = module.network.private_subnet_ids
  sg_batch_id          = module.network.sg_batch_id
  ecr_trainer_url      = module.storage.ecr_trainer_url
  trainer_image_tag    = var.trainer_image_tag
  spot_max_vcpus       = var.spot_max_vcpus
  ondemand_max_vcpus   = var.ondemand_max_vcpus
  instance_type        = var.batch_instance_type
  tracking_uri         = module.mlflow.tracking_uri
  artifacts_bucket     = module.storage.artifacts_bucket
  artifacts_bucket_arn = module.storage.artifacts_bucket_arn
  data_bucket          = module.storage.data_bucket
  data_bucket_arn      = module.storage.data_bucket_arn
  log_retention_days   = var.log_retention_days
}
```

> **Checkpoint**: batch depende de network (subnets/SG), storage (ECR
> + buckets) y mlflow (`tracking_uri`). El `tracking_uri` se inyecta
> como env var al job-def para que los containers de entrenamiento
> sepan a donde loguear runs sin hardcodearlo.

---

## 3.8 `modules/monitoring/` — SNS + alarmas

Genera **una alarma MAPE por variedad** (no hardcoded a POP como en V1).
La alarma escucha la custom metric que el trainer va a emitir en Parte 5.

### 3.8.1 `modules/monitoring/variables.tf`

```hcl
variable "project" { type = string }
variable "alert_email" { type = string }
variable "batch_job_queue_name" { type = string }
variable "alb_arn_suffix" {
  type        = string
  description = "Suffix del ALB ARN (formato 'app/<name>/<id>'). Usado por CloudWatch metrics."
}
variable "varieties" { type = list(string) }
variable "mape_alarm_threshold" { type = number }
variable "log_retention_days" { type = number }
```

> **Por que `alb_arn_suffix` y no `alb_arn`**: CloudWatch espera el
> suffix exacto en la dimension `LoadBalancer` (no el ARN completo).
> El recurso `aws_lb` expone `arn_suffix` como atributo nativo;
> extraerlo con `split()` desde el ARN es fragil ante cambios de
> formato. Usar el atributo del provider es la opcion correcta.

### 3.8.2 `modules/monitoring/main.tf`

> **Equivalente en AWS Console — el patron SNS + CloudWatch Alarms**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_sns_topic.alerts` | **SNS** | `SNS > Topics > Create topic`. **Type**: Standard. **Name**: `ml-training-alerts`. |
> | `aws_sns_topic_subscription.email` | **SNS** | Dentro del topic: `Create subscription`. **Protocol**: Email. **Endpoint**: `abantodca@gmail.com`. Status queda en **PendingConfirmation** hasta que clickees el mail "AWS Notification - Subscription Confirmation". |
> | `aws_cloudwatch_metric_alarm.batch_failed` | **CloudWatch** | `CloudWatch > Alarms > Create alarm > Select metric > Batch > By Job Queue`. Selecciona la metrica `FailedJobs` con dim `JobQueue=ml-training-job-queue-spot`. Statistic: Sum. Period: 5 min. Threshold: `>= 1`. Notification: SNS topic `ml-training-alerts`. |
> | `aws_cloudwatch_metric_alarm.mape_high` (1 por variedad) | **CloudWatch** | Mismo wizard pero `Custom namespace > ml-training/Training > MAPE`, dimension `variety=<X>`. Threshold: `> 20%`. **Treat missing data**: notBreaching (importante: si no hay datos, no dispares falsa alarma — por defecto Console pone "missing" que causa false positives). |
> | `aws_cloudwatch_metric_alarm.alb_5xx` | **CloudWatch** | Mismo wizard, namespace `AWS/ApplicationELB > Per AppELB Metrics`, metric `HTTPCode_Target_5XX_Count`. Threshold: `> 10` en 2 periodos consecutivos de 5 min. |
>
> **Conceptualmente — el pipeline de alertas**:
> - **SNS Topic** = "canal de notificacion" pub/sub. Cualquier service de AWS puede publicar mensajes; cualquier endpoint suscrito recibe copia. **Es la pieza central** — todas las alarmas (CloudWatch, Lambda dispatcher, EventBridge) postean aca, y un solo subscriber (tu email) recibe todo. Asi podes agregar Slack/PagerDuty mas adelante sin tocar las alarmas.
> - **CloudWatch Alarm** = evalua una metrica con una formula. Cuando la formula cambia de estado (`OK → ALARM`), publica un mensaje al SNS configurado en `alarm_actions`.
> - **Por que `for_each` en mape_high**: una alarma POR variedad (si entrenas 5 variedades, son 5 alarmas separadas). Asi un mail dice "MAPE de POP supero 20%", no "alguna metric agregada supero algo". Hace debug instantaneo.
> - **`treat_missing_data = notBreaching`**: si la metrica NO se publico (ej. no corriste training hoy), la alarma queda en `INSUFFICIENT_DATA`, no dispara. Default de Console es "missing breaches" que da falsa alarma cuando la app esta apagada.
> - **`evaluation_periods = 2` en ALB 5xx**: requiere 2 periodos de 5 min consecutivos > threshold para disparar. Evita falsa alarma por un spike transient (un solo error). Para MAPE no hace falta — el trainer publica 1 valor por run, asi que 1 periodo basta.

```hcl
# ----- SNS topic + suscripcion email ----------------------------------
resource "aws_sns_topic" "alerts" {
  name = "${var.project}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ----- Alarma 1: Batch job FAILED -------------------------------------
# CloudWatch publica metricas de Batch (FailedJobs por queue) cada 5 min.
resource "aws_cloudwatch_metric_alarm" "batch_failed" {
  alarm_name          = "${var.project}-batch-job-failed"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "FailedJobs"
  namespace           = "AWS/Batch"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "Al menos un Batch job fallo (no por Spot interrupt)"
  treat_missing_data  = "notBreaching"
  dimensions = {
    JobQueue = var.batch_job_queue_name
  }
  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# ----- Alarma 2: MAPE alto, por variedad ------------------------------
# Custom metric "MAPE" en namespace "${project}/Training", dimension
# `variety`. Emitida por el trainer (Parte 5).
resource "aws_cloudwatch_metric_alarm" "mape_high" {
  for_each = toset(var.varieties)

  alarm_name          = "${var.project}-mape-${lower(each.value)}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "MAPE"
  namespace           = "${var.project}/Training"
  period              = 3600 # 1h (MAPE se publica al final del run)
  statistic           = "Maximum"
  threshold           = var.mape_alarm_threshold
  alarm_description   = "MAPE de ${each.value} supero ${var.mape_alarm_threshold}%"
  treat_missing_data  = "notBreaching"
  dimensions = {
    variety = each.value
  }
  alarm_actions = [aws_sns_topic.alerts.arn]
}

# ----- Alarma 3: ALB 5xx -----------------------------------------------
resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${var.project}-alb-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 300
  statistic           = "Sum"
  threshold           = 10
  treat_missing_data  = "notBreaching"
  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }
  alarm_actions = [aws_sns_topic.alerts.arn]
}
```

### 3.8.3 `modules/monitoring/outputs.tf`

```hcl
output "sns_topic_arn" { value = aws_sns_topic.alerts.arn }
```

> **En consola AWS veras**:
> - SNS → Topics → `ml-training-alerts` con 1 subscription
>   (`Protocol=email`, `Endpoint=<alert_email>`, `Status=PendingConfirmation`
>   hasta que clickees el mail de §4.6).
> - CloudWatch → Alarms → **N + 2 alarmas** con prefijo `ml-training-`,
>   donde N = `length(var.varieties)`. El conteo escala automatic si
>   agregas/quitas variedades — `for_each` se reconcilia en el proximo
>   `terraform apply`:
>   - `ml-training-batch-failed` (Batch FailedJobs sum > 0 en 5 min) — 1 sola.
>   - `ml-training-mape-<variety>` — **una por variedad**
>     (`for_each = toset(var.varieties)`). Custom metric
>     namespace=`ml-training/Training`, dim=`variety`, threshold default
>     20%. En `INSUFFICIENT_DATA` hasta el primer training (esperable).
>   - `ml-training-alb-5xx` (HTTPCode_Target_5XX_Count del ALB) — 1 sola.
> - Cada alarma con `AlarmActions=[<topic-arn>]`. La de Batch tambien
>   tiene `OKActions` para mandar mail al recuperar.

### 3.8.4 Apendear `module "monitoring"` en `infra/envs/prod/main.tf`

Pegar AL FINAL de `infra/envs/prod/main.tf` (despues de
`module "batch"` de §3.7.5):

```hcl
# -------------------------------------------------------------------------
# Capa 6: Monitoring (SNS + alarmas CloudWatch)
# -------------------------------------------------------------------------
module "monitoring" {
  source = "../../modules/monitoring"

  project              = var.project
  alert_email          = var.alert_email
  batch_job_queue_name = module.batch.job_queue_spot
  alb_arn_suffix       = module.mlflow.alb_arn_suffix
  varieties            = var.varieties_allowed
  mape_alarm_threshold = var.mape_alarm_threshold
  log_retention_days   = var.log_retention_days
}
```

> **Checkpoint**: monitoring lee el nombre de la queue de `module.batch`
> (para alarmas de jobs failed) y el `alb_arn_suffix` de `module.mlflow`
> (para alarma 5XX del ALB). El `sns_topic_arn` que genera lo consumira
> `module.lambdas` en §3.9.7 (notifier).

---

## 3.9 `modules/lambdas/` — dispatcher + notifier

Dos Lambdas:

- **dispatcher**: validacion de payload + `boto3.client('batch').submit_job`.
  Acepta `varieties` (CSV), `tuning` (`smoke|dev|prod|prod_xl`), y opcional
  `s3_data_key`. La queue se elige por `tuning`: `prod_xl` -> ondemand,
  el resto -> spot.
- **notifier**: recibe eventos EventBridge "Batch Job State Change FAILED"
  y publica un mensaje a SNS con el log link directo.

> **Orden de pegado importante**: los `.tf` de §3.9.2 y §3.9.3 usan
> `data "archive_file"` para empaquetar `infra/lambdas/dispatcher.py` y
> `infra/lambdas/notifier.py`. Si haces `terraform plan` antes de crear
> esos `.py`, falla con "no such file or directory". Para evitarlo:
>
> 1. **Crear primero los `.py`** — saltar a §3.9.5 (dispatcher.py) y
>    §3.9.6 (notifier.py), pegar el codigo en
>    `infra/lambdas/dispatcher.py` y `infra/lambdas/notifier.py`.
> 2. **Luego volver aca** y pegar los `.tf` (3.9.1 -> 3.9.4).
>
> El orden de presentacion (`.tf` antes que `.py`) es para leer la
> arquitectura primero (variables -> resources -> outputs); el orden
> de creacion de archivos es al reves.

### 3.9.1 `modules/lambdas/variables.tf`

```hcl
variable "project" { type = string }
variable "job_queue_spot_arn" { type = string }
variable "job_queue_ondemand_arn" { type = string }
# *_name vars: el dispatcher.py / notifier.py usan los NAMES (no ARNs)
# para `batch.submit_job` / `batch.describe_jobs`. Antes el .tf construia
# `"${var.project}-job-queue-spot"` inline; ahora se reciben como input
# del envs/prod (wireado desde module.batch.job_queue_spot/ondemand).
variable "job_queue_spot_name" { type = string }
variable "job_queue_ondemand_name" { type = string }
variable "job_definition_name" { type = string }
variable "data_bucket" { type = string }
variable "varieties_allowed" { type = list(string) }
variable "sns_topic_arn" { type = string }
variable "batch_log_group_name" { type = string }
variable "log_retention_days" { type = number }
variable "lambdas_src_dir" { type = string }
```

### 3.9.2 `modules/lambdas/dispatcher.tf`

> **Equivalente en AWS Console — pieza por pieza del dispatcher**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `data "archive_file"` | **(local Terraform)** | NO es AWS — Terraform comprime localmente `dispatcher.py` → `dispatcher.zip`. En Console, vos tendrias que hacer el `zip` a mano antes de subir. |
> | `aws_iam_role.dispatcher` + inline policy | **IAM** | `IAM > Roles > Create role > AWS service > Lambda`. **Permissions**: inline policy con `batch:SubmitJob`, `batch:DescribeJobs` sobre las 2 queues + job-def, y `logs:Create*/PutLogEvents` para CloudWatch. **Name**: `ml-training-dispatcher`. |
> | `aws_cloudwatch_log_group.dispatcher` | **CloudWatch** | `Create log group`. Name: `/aws/lambda/ml-training-dispatcher`. (Lambda lo crea automaticamente la primera vez que loggeas, pero creandolo explicito te permite setear retention.) |
> | `aws_lambda_function.dispatcher` | **λ Lambda** | `Lambda > Functions > Create function > Author from scratch`. **Name**: `ml-training-dispatcher`. **Runtime**: Python 3.12. **Architecture**: x86_64. **Execution role**: el creado arriba. **Upload from**: `.zip file` → subir `dispatcher.zip`. **Handler**: `dispatcher.handler` (formato `<filename>.<function>`). **Configuration > General**: Timeout 60s, Memory 256 MB. **Configuration > Environment variables**: agregar las 6 vars (PROJECT, JOB_QUEUE_SPOT, etc.). |
>
> **Conceptualmente — Lambda como "REST endpoint para invocar Batch"**:
> - Lambda es **serverless compute** — pagas SOLO por ejecucion (no por estar prendido). Cuando alguien la invoca, AWS arranca un container con tu codigo, corre tu funcion, devuelve resultado, y apaga el container. ~100ms cold-start, ~10ms warm.
> - **Por que un dispatcher Lambda en vez de `aws batch submit-job` directo desde el cliente**:
>   - **Punto unico de validacion**: el dispatcher chequea que `varieties` esten en `VARIETIES_ALLOWED`, que `tuning` sea un preset valido, que `s3_data_key` exista. Si vos invocas batch directo, podrias submitir `--tuning xxx` sin querer y gastar EC2 corriendo basura.
>   - **Seleccion automatica de queue**: el dispatcher decide spot vs ondemand por `tuning`. Sin esto, cada caller tendria que conocer la regla.
>   - **Permission boundary**: el rol `gha-train` (Parte 3.11) tiene SOLO permiso de `lambda:InvokeFunction` sobre el dispatcher. No tiene `batch:SubmitJob` directo. Si comprometen GHA, el atacante solo puede invocar el dispatcher con payloads validos (no puede submitir jobs ad-hoc con imagenes raras).
> - **`source_code_hash`**: cuando cambia el .zip, Terraform lo detecta via hash y dispara redeploy. Sin esto, Terraform veria "el filename no cambio" y no haria nada (el zip se reconstruiria pero Lambda seguiria con la version vieja).

```hcl
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Empaca el codigo Python en zip
data "archive_file" "dispatcher" {
  type        = "zip"
  source_file = "${var.lambdas_src_dir}/dispatcher.py"
  output_path = "${path.module}/dispatcher.zip"
}

# IAM (trust policy compartida en infra/modules/_shared/assume-lambda.json)
resource "aws_iam_role" "dispatcher" {
  name               = "${var.project}-dispatcher"
  assume_role_policy = file("${path.module}/../_shared/assume-lambda.json")
}

resource "aws_iam_role_policy" "dispatcher" {
  role = aws_iam_role.dispatcher.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["batch:SubmitJob", "batch:DescribeJobs"]
        Resource = [
          var.job_queue_spot_arn,
          var.job_queue_ondemand_arn,
          # job-def ARN sin :revision para que matchee cualquier version
          "arn:aws:batch:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:job-definition/${var.job_definition_name}:*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "dispatcher" {
  name              = "/aws/lambda/${var.project}-dispatcher"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "dispatcher" {
  function_name    = "${var.project}-dispatcher"
  role             = aws_iam_role.dispatcher.arn
  runtime          = "python3.12"
  handler          = "dispatcher.handler"
  filename         = data.archive_file.dispatcher.output_path
  source_code_hash = data.archive_file.dispatcher.output_base64sha256
  timeout          = 60
  memory_size      = 256

  environment {
    variables = {
      PROJECT = var.project
      # AWS Batch SubmitJob/ListJobs aceptan ARN o name; usamos NAME.
      # Antes el .tf construia `"${var.project}-job-queue-spot"` inline;
      # ahora se recibe como input (var.job_queue_spot_name) wireado
      # desde module.batch.job_queue_spot — single source of truth.
      JOB_QUEUE_SPOT     = var.job_queue_spot_name
      JOB_QUEUE_ONDEMAND = var.job_queue_ondemand_name
      JOB_DEFINITION     = var.job_definition_name
      DATA_BUCKET        = var.data_bucket
      VARIETIES_ALLOWED  = join(",", var.varieties_allowed)
    }
  }

  depends_on = [aws_cloudwatch_log_group.dispatcher]
}
```

### 3.9.3 `modules/lambdas/notifier.tf`

> **Equivalente en AWS Console — Lambda + EventBridge trigger**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_iam_role.notifier` + inline policy | **IAM** | Wizard de Lambda role. **Permissions**: inline policy con `sns:Publish` (al topic), `batch:DescribeJobs`, `logs:*`. **Name**: `ml-training-notifier`. |
> | `aws_lambda_function.notifier` | **λ Lambda** | Mismo wizard que dispatcher. **Name**: `ml-training-notifier`. **Timeout**: 30s. **Memory**: 128 MB. Env: `SNS_TOPIC_ARN` + `BATCH_LOG_GROUP` (nombre real del log group, ej. `/aws/batch/ml-training`, propagado desde el output del modulo batch — antes era construido inline con `f"/aws/batch/{PROJECT}"`, frágil si cambia el patron). |
> | `aws_cloudwatch_event_rule.batch_failed` | **EventBridge** | `EventBridge > Rules > Create rule`. **Name**: `ml-training-batch-failed`. **Event bus**: default. **Rule type**: Rule with an event pattern. **Event source**: AWS services > AWS Batch > Batch Job State Change. **Specific status(es)**: FAILED. La consola te muestra un preview del JSON pattern. |
> | `aws_cloudwatch_event_target.notifier` | **EventBridge** | Dentro de la rule: `Add target > Lambda function > ml-training-notifier`. |
> | `aws_lambda_permission.notifier_eventbridge` | **λ Lambda** | NO existe en Console como recurso aparte — la consola lo crea **automaticamente** al asociar el target (te pide "Add permission"). En Terraform es explicito. |
>
> **Conceptualmente — el patron event-driven con EventBridge**:
> - **EventBridge** es el "bus de eventos" central de AWS. Casi todo servicio publica eventos automaticamente (Batch publica "Job State Change", EC2 publica "Instance State Change", S3 publica "Object Created", etc.). **Es gratis** publicar; pagas $1/M eventos consumidos.
> - **Rule** = filtro + accion. El `event_pattern` filtra (`{source: aws.batch, detail-type: Batch Job State Change, status: FAILED}`); el `target` es el destino que recibe el evento que matcheo (Lambda, SNS, SQS, Step Functions, etc.).
> - **Por que NO mandar de Batch → SNS directo**: SNS no permite filtrar/transformar el payload, ni hacer DescribeJobs. El Lambda notifier hace:
>   1. Recibe el evento (`{"jobId": "abc-123", "status": "FAILED"}`)
>   2. `batch.describe_jobs(jobs=[abc-123])` para sacar nombre, queue, log stream
>   3. Construye un mensaje legible: `"Job ml-training-POP-prod fallo. Ver logs: https://console.aws.amazon.com/cloudwatch/...?logStream=trainer/abc-123"`
>   4. `sns.publish(topic, message)` → te llega el mail con link directo al log
> - **`aws_lambda_permission`**: paso oculto pero critico. Lambda por default NO acepta invocaciones de nadie. Cada source (EventBridge, S3, API Gateway) debe agregar una "resource policy" explicita autorizando la invocation. En Console se hace automatico; en Terraform es manual y por eso aparece como recurso separado.

```hcl
data "archive_file" "notifier" {
  type        = "zip"
  source_file = "${var.lambdas_src_dir}/notifier.py"
  output_path = "${path.module}/notifier.zip"
}

resource "aws_iam_role" "notifier" {
  name               = "${var.project}-notifier"
  assume_role_policy = file("${path.module}/../_shared/assume-lambda.json")
}

resource "aws_iam_role_policy" "notifier" {
  role = aws_iam_role.notifier.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = var.sns_topic_arn
      },
      {
        Effect   = "Allow"
        Action   = ["batch:DescribeJobs"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "notifier" {
  name              = "/aws/lambda/${var.project}-notifier"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "notifier" {
  function_name    = "${var.project}-notifier"
  role             = aws_iam_role.notifier.arn
  runtime          = "python3.12"
  handler          = "notifier.handler"
  filename         = data.archive_file.notifier.output_path
  source_code_hash = data.archive_file.notifier.output_base64sha256
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      SNS_TOPIC_ARN   = var.sns_topic_arn
      BATCH_LOG_GROUP = var.batch_log_group_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.notifier]
}

# EventBridge rule: Batch Job State Change FAILED -> notifier
resource "aws_cloudwatch_event_rule" "batch_failed" {
  name        = "${var.project}-batch-failed"
  description = "Captura Batch jobs en estado FAILED"
  event_pattern = jsonencode({
    source        = ["aws.batch"]
    "detail-type" = ["Batch Job State Change"]
    detail = {
      status = ["FAILED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "notifier" {
  rule      = aws_cloudwatch_event_rule.batch_failed.name
  target_id = "notifier"
  arn       = aws_lambda_function.notifier.arn
}

resource "aws_lambda_permission" "notifier_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.batch_failed.arn
}
```

### 3.9.4 `modules/lambdas/outputs.tf`

```hcl
output "dispatcher_function_name" { value = aws_lambda_function.dispatcher.function_name }
output "dispatcher_function_arn" { value = aws_lambda_function.dispatcher.arn }
output "notifier_function_name" { value = aws_lambda_function.notifier.function_name }
```

### 3.9.5 `infra/lambdas/dispatcher.py`

```python
"""Lambda dispatcher: submit jobs a AWS Batch.

Payload aceptado:
{
  "varieties": "POP,JUPITER",      # CSV o "all"
  "tuning":    "prod",             # smoke|dev|prod|prod_xl
  "s3_data_key": "BD_HISTORICO_ACUMULADO.xlsx"   # opcional, default = ese mismo
}

Contrato del trainer (main.py):
- CMD ["--varieties","POP,JUPITER","--tuning","prod"]
- ENV S3_DATA_BUCKET, S3_DATA_KEY (para _hydrate_data_from_s3)
- ENV MLFLOW_TRACKING_URI, S3_ARTIFACTS_BUCKET, ... (ya en job-def)
"""

from __future__ import annotations

import json
import logging
import os
import re

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

batch = boto3.client("batch")

PROJECT            = os.environ["PROJECT"]
JOB_QUEUE_SPOT     = os.environ["JOB_QUEUE_SPOT"]
JOB_QUEUE_ONDEMAND = os.environ["JOB_QUEUE_ONDEMAND"]
JOB_DEFINITION     = os.environ["JOB_DEFINITION"]
DATA_BUCKET        = os.environ["DATA_BUCKET"]
VARIETIES_ALLOWED  = set(os.environ["VARIETIES_ALLOWED"].split(","))

TUNINGS = {"smoke", "dev", "prod", "prod_xl"}


def _normalize_varieties(raw: str) -> list[str]:
    if not raw:
        raise ValueError("varieties vacio")
    raw = raw.strip()
    if raw.lower() == "all":
        return sorted(VARIETIES_ALLOWED)
    items = [v.strip().upper() for v in raw.split(",") if v.strip()]
    bad = [v for v in items if v not in VARIETIES_ALLOWED]
    if bad:
        raise ValueError(f"variedades no permitidas: {bad}. Validas: {sorted(VARIETIES_ALLOWED)}")
    return items


def _validate_tuning(tuning: str) -> str:
    if tuning not in TUNINGS:
        raise ValueError(f"tuning invalido: {tuning}. Validos: {sorted(TUNINGS)}")
    return tuning


def _validate_key(key: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._/\-]+\.xlsx", key):
        raise ValueError(f"s3_data_key invalido: {key}")
    return key


def handler(event, _context):
    log.info("event: %s", json.dumps(event)[:1000])

    # EventBridge envuelve el payload en `detail`; manual invoke lo pasa raw.
    payload = event.get("detail", event) or {}

    try:
        varieties = _normalize_varieties(payload.get("varieties", ""))
        tuning    = _validate_tuning(payload.get("tuning", "prod"))
        s3_key    = _validate_key(payload.get("s3_data_key", "BD_HISTORICO_ACUMULADO.xlsx"))
    except ValueError as exc:
        log.error("validacion fallo: %s", exc)
        return {"statusCode": 400, "body": str(exc)}

    queue = JOB_QUEUE_ONDEMAND if tuning == "prod_xl" else JOB_QUEUE_SPOT
    job_name = f"{PROJECT}-{tuning}-{'-'.join(varieties)[:50]}"
    # sanitize: Batch acepta [a-zA-Z0-9_-], max 128
    job_name = re.sub(r"[^a-zA-Z0-9_-]", "-", job_name)[:128]

    response = batch.submit_job(
        jobName=job_name,
        jobQueue=queue,
        jobDefinition=JOB_DEFINITION,
        containerOverrides={
            "command": ["--varieties", ",".join(varieties), "--tuning", tuning],
            "environment": [
                {"name": "S3_DATA_BUCKET", "value": DATA_BUCKET},
                {"name": "S3_DATA_KEY",    "value": s3_key},
            ],
        },
        tags={"variety": ",".join(varieties), "tuning": tuning},
    )

    log.info("submit OK: jobId=%s queue=%s", response["jobId"], queue)
    return {
        "statusCode": 200,
        "body": {
            "jobId":    response["jobId"],
            "jobName":  response["jobName"],
            "queue":    queue,
            "varieties": varieties,
            "tuning":   tuning,
        },
    }
```

### 3.9.6 `infra/lambdas/notifier.py`

```python
"""Lambda notifier: traduce un evento de Batch FAILED a un email SNS legible."""

from __future__ import annotations

import json
import logging
import os

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

sns   = boto3.client("sns")
batch = boto3.client("batch")

SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
# AWS_REGION lo inyecta Lambda runtime automaticamente.
AWS_REGION = os.environ["AWS_REGION"]
# BATCH_LOG_GROUP es el name real (ej. "/aws/batch/ml-training"); el modulo
# batch lo expone como output y se pasa via env var. Antes se construia con
# f"/aws/batch/{PROJECT}" -> rompia silencioso si el log group cambiaba de patron.
BATCH_LOG_GROUP = os.environ["BATCH_LOG_GROUP"]


def _cw_url_encode(s: str) -> str:
    """CloudWatch UI hace doble URL-decode del log group/stream name.

    "/" se vuelve "$252F" (% URL-encoded a %25, luego %25 + 2F = $252F).
    """
    return s.replace("/", "$252F")


def handler(event, _context):
    log.info("event: %s", json.dumps(event)[:1500])

    detail = event.get("detail", {})
    job_id = detail.get("jobId")
    if not job_id:
        return {"statusCode": 400, "body": "no jobId in event"}

    job_name   = detail.get("jobName", "?")
    queue_arn  = detail.get("jobQueue", "?")
    reason     = detail.get("statusReason", "?")
    container  = detail.get("container", {})
    exit_code  = container.get("exitCode", "?")
    log_stream = container.get("logStreamName")

    log_url = "(no log stream)"
    if log_stream:
        log_url = (
            f"https://{AWS_REGION}.console.aws.amazon.com/cloudwatch/home"
            f"?region={AWS_REGION}#logsV2:log-groups/log-group/"
            f"{_cw_url_encode(BATCH_LOG_GROUP)}/log-events/"
            f"{_cw_url_encode(log_stream)}"
        )

    subject = f"[ml-training] Job FAILED: {job_name}"
    body = "\n".join([
        f"Job ID:    {job_id}",
        f"Job name:  {job_name}",
        f"Queue:     {queue_arn.rsplit('/', 1)[-1]}",
        f"Exit code: {exit_code}",
        f"Reason:    {reason}",
        f"Logs:      {log_url}",
    ])

    sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=body)
    log.info("notified jobId=%s", job_id)
    return {"statusCode": 200, "body": "notified"}
```

### 3.9.7 Apendear `module "lambdas"` en `infra/envs/prod/main.tf`

Pegar AL FINAL de `infra/envs/prod/main.tf` (despues de
`module "monitoring"` de §3.8.4):

```hcl
# -------------------------------------------------------------------------
# Capa 7: Lambdas (dispatcher + notifier)
# -------------------------------------------------------------------------
module "lambdas" {
  source = "../../modules/lambdas"

  project                 = var.project
  job_queue_spot_arn      = module.batch.job_queue_spot_arn
  job_queue_ondemand_arn  = module.batch.job_queue_ondemand_arn
  job_queue_spot_name     = module.batch.job_queue_spot
  job_queue_ondemand_name = module.batch.job_queue_ondemand
  job_definition_name     = module.batch.job_definition_name
  data_bucket             = module.storage.data_bucket
  varieties_allowed       = var.varieties_allowed
  sns_topic_arn           = module.monitoring.sns_topic_arn
  batch_log_group_name    = module.batch.log_group_name
  log_retention_days      = var.log_retention_days
  lambdas_src_dir         = "${path.module}/../../lambdas"
}
```

> **Checkpoint**: el `lambdas_src_dir` apunta a `infra/lambdas/`
> (donde estan los `.py` de §3.9.5 y §3.9.6). Si todavia no pegaste
> los `.py`, `terraform plan` truena al hacer `archive_file` del zip.
> Por eso §3.9 te dice **primero pegar los `.py`, despues los `.tf`**.

---

## 3.10 `modules/scheduler/` — auto on/off RDS + Fargate

Una Lambda + 2 crons EventBridge. La Lambda hace `start`/`stop` segun
el payload. Antes de stop, chequea Batch jobs RUNNING — si hay, posterga
(no apaga). Lockeado a PET (UTC-5).

> **Orden de pegado**: igual que §3.9, `modules/scheduler/main.tf`
> empaca `infra/lambdas/scheduler.py` con `data "archive_file"`. Pegar
> **primero** §3.10.4 (`scheduler.py`) en `infra/lambdas/scheduler.py`,
> y **despues** §3.10.1-§3.10.3 (los `.tf`). Asi el `terraform plan`
> de §3.12 no truena por archivo inexistente.

### 3.10.1 `modules/scheduler/variables.tf`

```hcl
variable "project" { type = string }
variable "ecs_cluster_name" { type = string }
variable "ecs_service_name_mlflow" { type = string }
variable "ecs_service_name_reports" { type = string }
variable "rds_instance_id" { type = string }
# *_name vars: el scheduler.py llama batch.list_jobs(jobQueue=NAME)
# para detectar jobs RUNNING antes de apagar RDS. Antes el .tf
# construia los nombres inline; ahora se reciben como input desde
# envs/prod (module.batch.job_queue_spot / job_queue_ondemand).
variable "job_queue_spot_name" { type = string }
variable "job_queue_ondemand_name" { type = string }
variable "work_start_hour_local" { type = number }
variable "work_end_hour_local" { type = number }
variable "tz_offset_hours" {
  type    = number
  default = -5 # PET (Peru)
}
variable "workdays_cron" {
  type    = string
  default = "MON,WED,FRI" # Patch 13.1: solo L/Mi/V (antes: "MON-FRI")
}
variable "log_retention_days" { type = number }
variable "lambdas_src_dir" { type = string }
```

### 3.10.2 `modules/scheduler/main.tf`

3 sub-bloques al mismo `modules/scheduler/main.tf`:

#### 3.10.2.a — Lambda function + IAM

Empaqueta `infra/lambdas/scheduler.py` (que ya creaste antes — ver
callout arriba). IAM con scope a ECS update-service, RDS start/stop,
y Batch describe — todo `Resource="*"` porque los recursos del proyecto
son los unicos en la cuenta con esos names; refinable a ARN especifico
en hardening (§10).

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_iam_role.scheduler` + inline policy | **IAM** | `IAM > Roles > Create role > Lambda`. **Permissions**: inline policy con `ecs:UpdateService/DescribeServices`, `rds:StartDBInstance/StopDBInstance/DescribeDBInstances`, `batch:ListJobs/DescribeJobs`, `logs:*`. **Name**: `ml-training-scheduler`. |
> | `aws_lambda_function.scheduler` | **λ Lambda** | `Create function > Author from scratch`. **Name**: `ml-training-scheduler`. **Runtime**: Python 3.12. **Execution role**: el de arriba. Subir `scheduler.zip`. **Handler**: `scheduler.handler`. **Timeout**: 300s (la espera del RDS start cold-start es ~3-5 min, por eso 5 min). **Memory**: 256 MB. **Env vars**: PROJECT, ECS_CLUSTER, ECS_SVC_MLFLOW, ECS_SVC_REPORTS, RDS_INSTANCE, JOB_QUEUE_SPOT, JOB_QUEUE_ONDEMAND. |
>
> **Conceptualmente — por que UN Lambda con payload `action`, no DOS Lambdas**:
> - Vos podrias tener `scheduler-start.py` y `scheduler-stop.py` separados. Pero el codigo compartido (autenticacion ECS, espera de healthy, manejo de errores) seria duplicado.
> - **El patron usado**: una sola Lambda que recibe `{"action": "start"}` o `{"action": "stop"}`. Adentro hay un dispatcher (`if action == "start": _start_all()`). Asi reusas helpers + 1 set de IAM + 1 log group.
> - **Por que `timeout=300`**: RDS start cold no es instantaneo. La Lambda hace `start_db_instance` (~10s para que arranque la operacion) + opcionalmente espera a que pase a `available` (waiter `wait_until_db_instance_available` puede tardar 3-5 min). Si el timeout fuera 60s default, la Lambda timeoutearia antes de confirmar RDS healthy.
> - **`Resource="*"` en IAM**: simplifica policy pero es laxo. En hardening (Parte 10) se reemplaza por ARNs especificos: `arn:aws:rds:us-east-1:...:db:ml-training-mlflow`, etc. Aca acepta `*` porque los nombres de los recursos son unicos al proyecto.

```hcl
locals {
  start_hour_utc = (var.work_start_hour_local - var.tz_offset_hours + 24) % 24
  stop_hour_utc  = (var.work_end_hour_local - var.tz_offset_hours + 24) % 24
}

data "archive_file" "scheduler" {
  type        = "zip"
  source_file = "${var.lambdas_src_dir}/scheduler.py"
  output_path = "${path.module}/scheduler.zip"
}

# Trust policy compartida en infra/modules/_shared/assume-lambda.json
resource "aws_iam_role" "scheduler" {
  name               = "${var.project}-scheduler"
  assume_role_policy = file("${path.module}/../_shared/assume-lambda.json")
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "rds:StartDBInstance", "rds:StopDBInstance", "rds:DescribeDBInstances"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["batch:ListJobs", "batch:DescribeJobs"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "scheduler" {
  name              = "/aws/lambda/${var.project}-scheduler"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "scheduler" {
  function_name    = "${var.project}-scheduler"
  role             = aws_iam_role.scheduler.arn
  runtime          = "python3.12"
  handler          = "scheduler.handler"
  filename         = data.archive_file.scheduler.output_path
  source_code_hash = data.archive_file.scheduler.output_base64sha256
  timeout          = 900 # Patch 13.3: 15 min (antes 300). Cubre RDS cold start (~5-8 min) + wait MLflow.
  memory_size      = 256

  environment {
    variables = {
      PROJECT            = var.project
      ECS_CLUSTER        = var.ecs_cluster_name
      ECS_SVC_MLFLOW     = var.ecs_service_name_mlflow
      ECS_SVC_REPORTS    = var.ecs_service_name_reports
      RDS_INSTANCE       = var.rds_instance_id
      # Antes los names se construian inline (`"${var.project}-job-queue-spot"`);
      # ahora vienen como input wireado desde module.batch en envs/prod.
      JOB_QUEUE_SPOT     = var.job_queue_spot_name
      JOB_QUEUE_ONDEMAND = var.job_queue_ondemand_name
      # Patch 13.1: propagar workdays + ventana al _keepstop (sino el
      # martes/jueves queda "dentro de ventana" y nunca re-para el RDS).
      WORKDAYS_CRON  = var.workdays_cron
      WORK_START_UTC = tostring(local.start_hour_utc)
      WORK_END_UTC   = tostring(local.stop_hour_utc)
    }
  }

  depends_on = [aws_cloudwatch_log_group.scheduler]
}
```

#### 3.10.2.b — EventBridge rules (start, stop, keepstop)

3 crons: `start` (8 AM PET), `stop` (12 PM PET), `keepstop` (cada 6h
defensa contra el auto-arranque de RDS post-7-dias-stopped). El offset
PET→UTC se calcula en `locals` y se enchufa al `cron(0 H ? * MON-FRI *)`.

> **Equivalente en AWS Console**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_cloudwatch_event_rule.start` | **EventBridge** | `EventBridge > Rules > Create rule`. **Name**: `ml-training-start`. **Event bus**: default. **Rule type**: Schedule. **Schedule pattern**: A fine-grained schedule that runs at a specific time. **Cron expression**: `cron(0 13 ? * MON-FRI *)` (13 UTC = 08:00 PET). |
> | `aws_cloudwatch_event_target.start` | **EventBridge** | Dentro de la rule: `Add target > Lambda function > ml-training-scheduler`. **Configure target input**: Constant (JSON text): `{"action": "start"}`. |
> | `aws_cloudwatch_event_rule.stop` + target | **EventBridge** | Mismo wizard, name `-stop`, cron `cron(0 17 ? * MON-FRI *)` (17 UTC = 12:00 PET), input `{"action":"stop"}`. |
> | `aws_cloudwatch_event_rule.rds_keepstop` + target | **EventBridge** | Mismo wizard, name `-rds-keepstop`, **Schedule pattern**: A schedule that runs at a regular rate. **Rate expression**: `rate(6 hours)`. Input `{"action":"keepstop"}`. |
>
> **Conceptualmente — por que 3 rules y no 1 sola**:
> - Cada rule tiene 1 propósito y 1 cron expression. EventBridge cron es **sin overlap por defecto** — si fueran 1 sola rule con multiple targets, los 3 inputs se ejecutarian en cada tick. No es lo que queremos.
> - **Cron de EventBridge**: formato `cron(min hour day-of-month month day-of-week year)` (6 campos, NO los 5 de Linux). El `?` significa "no me importa" — en `day-of-month` lo usamos cuando especificamos `day-of-week`, y viceversa (mutuamente excluyentes en EventBridge).
> - **`MON-FRI` vs `MON,WED,FRI`**: cualquier subset funciona. En Parte 13.1 hay un patch para customizar a `MON,WED,FRI` si querés solo entrenar 3 dias.
> - **Por que `rate(6 hours)` para keepstop**: RDS tiene un comportamiento traidor — si lo dejas `stopped` mas de 7 dias, AWS **lo enciende automaticamente** "por mantenimiento". Esto te factura ~$15 sorpresa al mes. El keepstop cron corre cada 6h y si encuentra RDS en `available` fuera de la ventana laboral, lo vuelve a apagar.

```hcl
resource "aws_cloudwatch_event_rule" "start" {
  name                = "${var.project}-start"
  description         = "L-V ${var.work_start_hour_local}:00 PET start RDS+Fargate"
  schedule_expression = "cron(0 ${local.start_hour_utc} ? * ${var.workdays_cron} *)"
}

resource "aws_cloudwatch_event_target" "start" {
  rule      = aws_cloudwatch_event_rule.start.name
  target_id = "scheduler-start"
  arn       = aws_lambda_function.scheduler.arn
  input     = jsonencode({ action = "start" })
}

# ----- EventBridge: cron STOP L-V <stop_hour_utc>:00 -----------------
resource "aws_cloudwatch_event_rule" "stop" {
  name                = "${var.project}-stop"
  description         = "L-V ${var.work_end_hour_local}:00 PET stop RDS+Fargate"
  schedule_expression = "cron(0 ${local.stop_hour_utc} ? * ${var.workdays_cron} *)"
}

resource "aws_cloudwatch_event_target" "stop" {
  rule      = aws_cloudwatch_event_rule.stop.name
  target_id = "scheduler-stop"
  arn       = aws_lambda_function.scheduler.arn
  input     = jsonencode({ action = "stop" })
}

# ----- Cron extra: cada 6h chequea RDS y lo re-stop si quedo RUNNING --
# (necesario porque RDS auto-arranca despues de 7 dias stopped)
resource "aws_cloudwatch_event_rule" "rds_keepstop" {
  name                = "${var.project}-rds-keepstop"
  description         = "Cada 6h: re-stop RDS si quedo RUNNING fuera de ventana"
  schedule_expression = "rate(6 hours)"
}

resource "aws_cloudwatch_event_target" "rds_keepstop" {
  rule      = aws_cloudwatch_event_rule.rds_keepstop.name
  target_id = "scheduler-keepstop"
  arn       = aws_lambda_function.scheduler.arn
  input     = jsonencode({ action = "keepstop" })
}
```

#### 3.10.2.c — Permissions (EventBridge → Lambda)

EventBridge no puede invocar Lambdas por defecto; cada rule necesita
su propia `lambda_permission` con `source_arn` matching. 3 rules =
3 permissions.

> **Equivalente en AWS Console**:
> 
> En Console, cuando agregas una rule como target de una Lambda, el wizard pregunta automaticamente "Add the necessary permissions for the target to be invoked by this rule?" → al hacer click en "Confirm", la consola agrega esta resource policy a la Lambda. Por eso en Console no ves recursos `aws_lambda_permission` aparte — son **invisibles, gestionados por el wizard**.
>
> En Terraform es **explicito** porque Terraform no infiere ese tipo de "side effect" — necesita un recurso declarativo. Si te olvidas el `aws_lambda_permission`, EventBridge dispara la rule, pero Lambda devuelve `403 AccessDenied` y nunca corre.
>
> **Conceptualmente — el modelo de permisos cruzados de AWS**:
> Hay 2 lados que necesitan autorizar la invocacion:
> - **Lado del invocador (EventBridge)**: tiene una "rule role" con permiso `lambda:InvokeFunction` (no es lo que ves aca — es implicito en EventBridge, no requiere tf).
> - **Lado del invocado (Lambda)**: tiene una "resource policy" que dice "permito que `events.amazonaws.com` (con source_arn matching mi rule) me invoque". **ESTE** es el `aws_lambda_permission`.
> 
> Sin el resource policy, Lambda rechaza la invocacion aunque el caller tenga perms IAM. Es el mismo patron que vimos en `aws_lambda_permission.notifier_eventbridge` (Parte 3.9.3).

```hcl
resource "aws_lambda_permission" "start" {
  statement_id  = "AllowStart"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.start.arn
}

resource "aws_lambda_permission" "stop" {
  statement_id  = "AllowStop"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.stop.arn
}

resource "aws_lambda_permission" "keepstop" {
  statement_id  = "AllowKeepstop"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.rds_keepstop.arn
}
```

> **Checkpoint despues de 3.10.2.a-c**: `terraform fmt
> infra/modules/scheduler/main.tf`.

### 3.10.3 `modules/scheduler/outputs.tf`

```hcl
output "function_name" { value = aws_lambda_function.scheduler.function_name }
output "function_arn" { value = aws_lambda_function.scheduler.arn }
```

### 3.10.4 `infra/lambdas/scheduler.py`

> **Variante con dias custom (L/Mi/V o cualquier subset)**: el codigo
> de abajo asume `weekday < 5` hardcoded — funciona para el default
> `MON-FRI`. Si necesitas otros workdays (ej. solo lunes/miercoles/
> viernes), aplicar el patch de **§13.1** *despues* de Parte 4. El patch
> reemplaza el hardcode por un parser de la env var `WORKDAYS_CRON`.

```python
"""Lambda scheduler: start/stop RDS + Fargate.

Acciones:
- start:    arranca RDS + ECS services desired_count=1
- stop:     baja ECS services a 0 + para RDS. Antes chequea Batch jobs RUNNING.
- keepstop: cada 6h. Si RDS quedo RUNNING fuera de ventana, lo re-para.
"""

from __future__ import annotations

import logging
import os
import time

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

ecs   = boto3.client("ecs")
rds   = boto3.client("rds")
batch = boto3.client("batch")

ECS_CLUSTER        = os.environ["ECS_CLUSTER"]
ECS_SVC_MLFLOW     = os.environ["ECS_SVC_MLFLOW"]
ECS_SVC_REPORTS    = os.environ["ECS_SVC_REPORTS"]
RDS_INSTANCE       = os.environ["RDS_INSTANCE"]
JOB_QUEUE_SPOT     = os.environ["JOB_QUEUE_SPOT"]
JOB_QUEUE_ONDEMAND = os.environ["JOB_QUEUE_ONDEMAND"]


def _running_jobs() -> list[str]:
    """IDs de jobs en estado RUNNING o RUNNABLE en cualquiera de las queues."""
    ids: list[str] = []
    for queue in (JOB_QUEUE_SPOT, JOB_QUEUE_ONDEMAND):
        for status in ("RUNNING", "RUNNABLE", "STARTING"):
            resp = batch.list_jobs(jobQueue=queue, jobStatus=status)
            ids.extend(j["jobId"] for j in resp.get("jobSummaryList", []))
    return ids


def _start():
    log.info("=== START ===")
    # ECS: desired_count = 1 (mlflow + reports)
    for svc in (ECS_SVC_MLFLOW, ECS_SVC_REPORTS):
        ecs.update_service(cluster=ECS_CLUSTER, service=svc, desiredCount=1)
        log.info("ecs %s -> desiredCount=1", svc)

    # RDS: start si esta stopped
    db = rds.describe_db_instances(DBInstanceIdentifier=RDS_INSTANCE)["DBInstances"][0]
    state = db["DBInstanceStatus"]
    if state == "stopped":
        rds.start_db_instance(DBInstanceIdentifier=RDS_INSTANCE)
        log.info("rds start_db_instance ack (cold start ~5 min)")
    else:
        log.info("rds en estado %s (skip start)", state)


def _stop():
    log.info("=== STOP ===")
    running = _running_jobs()
    if running:
        log.warning(
            "Batch jobs activos (%d): %s. Postponiendo stop hasta proximo cron.",
            len(running), running[:5]
        )
        return

    # ECS: desired_count = 0
    for svc in (ECS_SVC_MLFLOW, ECS_SVC_REPORTS):
        ecs.update_service(cluster=ECS_CLUSTER, service=svc, desiredCount=0)
        log.info("ecs %s -> desiredCount=0", svc)

    # RDS: stop si esta RUNNING
    db = rds.describe_db_instances(DBInstanceIdentifier=RDS_INSTANCE)["DBInstances"][0]
    state = db["DBInstanceStatus"]
    if state == "available":
        rds.stop_db_instance(DBInstanceIdentifier=RDS_INSTANCE)
        log.info("rds stop_db_instance ack")
    else:
        log.info("rds en estado %s (skip stop)", state)


def _keepstop():
    """Defense: si RDS quedo RUNNING fuera de ventana, re-pararlo."""
    log.info("=== KEEPSTOP ===")
    # Heuristica simple: chequear si estamos en ventana laboral PET.
    # 13:00 UTC = 08:00 PET; 17:00 UTC = 12:00 PET.
    utc_hour = time.gmtime().tm_hour
    weekday = time.gmtime().tm_wday   # 0=lunes
    in_window = (weekday < 5) and (13 <= utc_hour < 17)
    if in_window:
        log.info("dentro de ventana (UTC=%02d:00, weekday=%d), skip", utc_hour, weekday)
        return

    db = rds.describe_db_instances(DBInstanceIdentifier=RDS_INSTANCE)["DBInstances"][0]
    state = db["DBInstanceStatus"]
    if state == "available":
        # Antes de para r, verificar Batch (igual que stop normal)
        running = _running_jobs()
        if running:
            log.warning("Batch jobs activos, skip keepstop")
            return
        rds.stop_db_instance(DBInstanceIdentifier=RDS_INSTANCE)
        log.info("rds re-stopped por keepstop")
    else:
        log.info("rds en estado %s (skip)", state)


def handler(event, _context):
    action = (event or {}).get("action", "stop")
    if action == "start":
        _start()
    elif action == "stop":
        _stop()
    elif action == "keepstop":
        _keepstop()
    else:
        raise ValueError(f"action desconocida: {action}")
    return {"statusCode": 200, "body": action}
```

### 3.10.5 Apendear `module "scheduler"` en `infra/envs/prod/main.tf`

Pegar AL FINAL de `infra/envs/prod/main.tf` (despues de
`module "lambdas"` de §3.9.7):

```hcl
# -------------------------------------------------------------------------
# Capa 8: Scheduler (auto on/off RDS + Fargate)
# -------------------------------------------------------------------------
module "scheduler" {
  source = "../../modules/scheduler"

  project                  = var.project
  ecs_cluster_name         = module.mlflow.cluster_name
  ecs_service_name_mlflow  = module.mlflow.service_name
  ecs_service_name_reports = module.reports.service_name
  rds_instance_id          = module.mlflow.rds_instance_id
  job_queue_spot_name      = module.batch.job_queue_spot
  job_queue_ondemand_name  = module.batch.job_queue_ondemand
  work_start_hour_local    = var.work_start_hour_local
  work_end_hour_local      = var.work_end_hour_local
  log_retention_days       = var.log_retention_days
  lambdas_src_dir          = "${path.module}/../../lambdas"
}
```

> **Checkpoint**: scheduler consume `cluster_name` / `service_name` /
> `rds_instance_id` para escalar Fargate a 0 y parar RDS fuera de
> horario laboral. Igual que §3.9.7, depende de que `scheduler.py`
> (§3.10.4) ya este pegado.

---

## 3.11 `modules/cicd/` — OIDC trust + GHA roles

Dos roles: `gha-deploy` (terraform apply, push ECR) y `gha-train`
(invoke Lambda dispatcher). Trust policy especifica `repo:org/repo:*`
para evitar que cualquier repo pueda asumir.

### 3.11.1 `modules/cicd/variables.tf`

```hcl
variable "project" { type = string }
variable "github_org" { type = string }
variable "github_repo" { type = string }
variable "oidc_provider_arn" { type = string }
variable "artifacts_bucket_arn" { type = string }
variable "data_bucket_arn" { type = string }
variable "ecr_trainer_arn" { type = string }
variable "job_queue_spot_arn" { type = string }
variable "job_queue_ondemand_arn" { type = string }
variable "job_definition_arn" { type = string }
```

### 3.11.2 `modules/cicd/main.tf`

> **Equivalente en AWS Console — los 2 IAM Roles con trust OIDC**:
>
> | Recurso Terraform | Servicio | Que harias click-a-click |
> |---|---|---|
> | `aws_iam_role.deploy` con trust OIDC | **IAM** | `IAM > Roles > Create role`. **Trusted entity type**: **Web identity** (NO "AWS service"). **Identity provider**: `token.actions.githubusercontent.com` (el OIDC creado en Parte 2.5). **Audience**: `sts.amazonaws.com`. **GitHub organization**: `<tu-org>`. **GitHub repository**: `<tu-repo>`. **GitHub branch**: deja vacio para `*`. La Console te genera el JSON con `StringLike` sobre `sub = repo:org/repo:*`. **Permissions**: inline policy con todo lo de Terraform apply + ECR push. **Name**: `ml-training-gha-deploy`. |
> | `aws_iam_role.train` con mismo trust | **IAM** | Mismo wizard de Web identity. **Permissions**: SOLO `lambda:InvokeFunction` sobre el dispatcher + `batch:Describe/ListJobs` + `logs:GetLogEvents`. **Name**: `ml-training-gha-train`. |
>
> **Conceptualmente — el flujo OIDC paso a paso (lo que va a pasar cuando GHA invoca esto)**:
> 1. **GitHub Actions arranca un workflow** (ej. push a `main`). En el job pones `permissions: id-token: write`.
> 2. **GH genera un JWT** firmado con su clave privada. El JWT contiene claims: `iss=token.actions.githubusercontent.com`, `aud=sts.amazonaws.com`, `sub=repo:mi-org/ml_training:ref:refs/heads/main`, `repository=mi-org/ml_training`, `run_id=...`, etc.
> 3. **El step `aws-actions/configure-aws-credentials@v4`** toma ese JWT y lo manda a STS: `sts:AssumeRoleWithWebIdentity` con `RoleArn=ml-training-gha-deploy`, `WebIdentityToken=<JWT>`.
> 4. **STS valida el JWT**:
>    - Llama al OIDC discovery endpoint de GH (`https://token.actions.githubusercontent.com/.well-known/openid-configuration`) para obtener la public key.
>    - Verifica la firma del JWT con esa key.
>    - Verifica los claims contra la **trust policy** del rol: `aud == "sts.amazonaws.com"` y `sub LIKE "repo:mi-org/ml_training:*"` .
> 5. **STS devuelve credenciales temporales** (~1 hora) con los permisos del rol. GHA las usa para `aws ecr push`, `terraform apply`, etc.
> 6. Despues de 1 hora las creds expiran. **No hay secrets de larga duracion guardados en GitHub** — esto es la GRAN VENTAJA vs el modelo viejo (`AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` como GH Secrets, eternos, expuestos en cada workflow run).
> - **Por que el trust `sub = repo:org/repo:*`**: bloquea a otros repos de la misma cuenta GH a asumir el rol. Si solo pusieramos `aud = sts.amazonaws.com` (que es shared a nivel cuenta), CUALQUIER repo en cualquier org de GitHub podria asumir el rol. El `sub` constraint es lo que limita a tu repo especifico.
> - **`gha-deploy` es PODEROSO**: tiene `iam:*`, `ec2:*`, `rds:*`, etc. → Si comprometen el OIDC trust, pueden destruir toda la infra y escalar a admin de la cuenta. Por eso branch protection (Parte 6.6) + GitHub Environments con manual approval (Parte 6.5) son CRITICOS — son las unicas barreras entre `git push` y `terraform destroy`.
> - **`gha-train` es MINIMO**: solo invoca el dispatcher Lambda. Si comprometen este rol, lo peor que pueden hacer es submitir un job de training (gasto controlado por el `dispatcher.py` que valida varieties + tuning).

```hcl
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  # Trust policy GHA-OIDC compartido entre gha-deploy y gha-train.
  # El template vive en infra/modules/_shared/assume-github-oidc.json.tftpl
  # — provider_arn / org / repo se inyectan via templatefile().
  # Mismo template usado por modules/consumer-iam (otro repo, mismo shape).
  gha_oidc_trust = templatefile("${path.module}/../_shared/assume-github-oidc.json.tftpl", {
    provider_arn = var.oidc_provider_arn
    org          = var.github_org
    repo         = var.github_repo
  })
}

# ----- Role 1: gha-deploy (CI workflows que aplican terraform + push ECR)
resource "aws_iam_role" "deploy" {
  name               = "${var.project}-gha-deploy"
  assume_role_policy = local.gha_oidc_trust
}

resource "aws_iam_role_policy" "deploy" {
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Terraform: state remoto
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"]
        Resource = [
          "arn:aws:s3:::${var.project}-tfstate-*",
          "arn:aws:s3:::${var.project}-tfstate-*/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:DescribeTable"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.project}-tflock"
      },
      # ECR: push de las 3 imagenes
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage"
        ]
        Resource = "*" # ECR scoping necesita el endpoint para auth, * es estandar
      },
      # Terraform: leer/escribir resources (scope intencionalmente amplio para que
      # `terraform apply` funcione sobre TODOS los modulos. En produccion mas
      # estricta, dividir en roles plan-only + apply-only).
      #
      # BLAST RADIUS de este statement: un atacante que comprometa el OIDC
      # trust (ej. fork con write a `main`, o GitHub actions de un usuario
      # con permisos en el repo) puede:
      #   - destruir TODA la infra del proyecto (terraform destroy desde CI).
      #   - crear nuevos IAM roles (iam:*) y escalar a admin de la cuenta.
      #   - leer Secrets Manager (incluido el RDS password).
      # MITIGACIONES en uso:
      #   - trust policy con `sub = "repo:org/repo:*"` (solo este repo).
      #   - branch protection en main (§6.6) + required reviewers.
      #   - GitHub Environment "production" con manual approval (§6.5).
      # Refinable en §10 (hardening): partir en deploy-plan-only + apply
      # con CODEOWNERS, o restringir Resource por modulo via tags.
      {
        Effect = "Allow"
        Action = [
          "ec2:*", "vpc:*", "iam:*", "rds:*", "logs:*",
          "ecs:*", "elasticloadbalancing:*", "servicediscovery:*",
          "batch:*", "lambda:*", "events:*", "sns:*",
          "cloudwatch:*", "secretsmanager:*", "kms:*",
          "s3:GetBucketLocation", "s3:ListAllMyBuckets",
          "s3:CreateBucket", "s3:DeleteBucket", "s3:PutBucket*", "s3:GetBucket*",
          "ecr:*"
        ]
        Resource = "*"
      }
    ]
  })
}

# ----- Role 2: gha-train (solo invocar Lambda dispatcher) -------------
resource "aws_iam_role" "train" {
  name               = "${var.project}-gha-train"
  assume_role_policy = local.gha_oidc_trust
}

resource "aws_iam_role_policy" "train" {
  role = aws_iam_role.train.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        # Patch 13.2: gha-train tambien invoca scheduler para wake/stop
        # en el workflow auto-train-on-push.yml.
        Resource = [
          "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project}-dispatcher",
          "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project}-scheduler"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["batch:DescribeJobs", "batch:ListJobs"]
        Resource = "*"
      },
      {
        # Patch 13.2: chequear estado RDS antes de wake
        Effect   = "Allow"
        Action   = ["rds:DescribeDBInstances"]
        Resource = "*"
      },
      {
        # Patch 13.2: chequear estado de los services Fargate
        Effect   = "Allow"
        Action   = ["ecs:DescribeServices"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:GetLogEvents", "logs:DescribeLogStreams"]
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/batch/${var.project}*"
      }
    ]
  })
}
```

### 3.11.3 `modules/cicd/outputs.tf`

```hcl
output "gha_deploy_role_arn" { value = aws_iam_role.deploy.arn }
output "gha_train_role_arn" { value = aws_iam_role.train.arn }
```

> **En consola AWS veras**:
> - IAM → Roles → `ml-training-gha-deploy` y `ml-training-gha-train`.
>   Ambas con trust policy que confia en el OIDC provider de §2.5 y
>   limita el `sub` a `repo:<github_org>/<github_repo>:*`.
> - IAM → Roles → `gha-deploy` → Permissions tab: inline policy con
>   ec2/iam/s3/ecr/ecs/batch/lambda/cloudwatch/logs/events/sns
>   (scope amplio para que `terraform apply` pueda crear/modificar
>   cualquier modulo). **Blast radius**: si alguien compromete el OIDC
>   trust (e.g., un fork con write a `main`), puede destruir toda la
>   infra — por eso branch protection (§6.6) es load-bearing.
> - IAM → Roles → `gha-train` → Permissions: solo `lambda:InvokeFunction`
>   sobre el dispatcher. Scope minimo intencional.
> - Estos roles son los `vars.AWS_GHA_DEPLOY_ROLE_ARN` /
>   `AWS_GHA_TRAIN_ROLE_ARN` que se setean con `gh variable set` en §6.1.

---

### 3.11.4 Apendear `module "cicd"` en `infra/envs/prod/main.tf`

Ultimo bloque. Pegar AL FINAL de `infra/envs/prod/main.tf` (despues
de `module "scheduler"` de §3.10.5):

```hcl
# -------------------------------------------------------------------------
# Capa 9: CI/CD (GHA IAM roles confiando en OIDC)
# -------------------------------------------------------------------------
module "cicd" {
  source = "../../modules/cicd"

  project                = var.project
  github_org             = var.github_org
  github_repo            = var.github_repo
  oidc_provider_arn      = data.aws_iam_openid_connect_provider.github.arn
  artifacts_bucket_arn   = module.storage.artifacts_bucket_arn
  data_bucket_arn        = module.storage.data_bucket_arn
  ecr_trainer_arn        = module.storage.ecr_trainer_arn
  job_queue_spot_arn     = module.batch.job_queue_spot_arn
  job_queue_ondemand_arn = module.batch.job_queue_ondemand_arn
  job_definition_arn     = module.batch.job_definition_arn
}
```

> **Checkpoint final**: con este bloque pegado, el `main.tf` esta
> **completo** (los 9 modulos + los 3 `data` sources). En §3.12 viene
> la validacion sintactica integrada (`terraform fmt -recursive` y
> `terraform validate`) antes de pasar a Parte 4 (apply real).

---

## 3.11.5 `modules/consumer-iam/` — Rol OIDC para repo consumer (Patch 13.5)

Rol IAM que el repo consumer (FastAPI/Streamlit que sirve modelos) asume via
GitHub OIDC para descargar artifacts de S3 read-only. Separado de `cicd/`
porque vive con permisos distintos y trust hacia otro repo.

Las vars `consumer_org` y `consumer_repo` ya estan declaradas en
`envs/prod/variables.tf` (Patch 13.5), y el OIDC provider es el mismo
`data "aws_iam_openid_connect_provider" "github"` que usa `cicd/` (§3.11),
asi que no hay que crear nada nuevo a nivel envs.

### 3.11.5.1 `modules/consumer-iam/variables.tf`

```hcl
variable "project" { type = string }
variable "artifacts_bucket_arn" { type = string }
variable "consumer_oidc_arn" { type = string }
variable "consumer_org" { type = string }
variable "consumer_repo" { type = string }
```

### 3.11.5.2 `modules/consumer-iam/main.tf`

```hcl
# Patch 13.5: rol que el repo consumer (FastAPI/Streamlit) asume via OIDC
# para descargar artifacts (modelos) desde S3 read-only.

resource "aws_iam_role" "consumer" {
  name = "${var.project}-consumer"
  assume_role_policy = templatefile("${path.module}/../_shared/assume-github-oidc.json.tftpl", {
    provider_arn = var.consumer_oidc_arn
    org          = var.consumer_org
    repo         = var.consumer_repo
  })
}

resource "aws_iam_role_policy" "consumer" {
  role = aws_iam_role.consumer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.artifacts_bucket_arn, "${var.artifacts_bucket_arn}/*"]
      }
    ]
  })
}
```

### 3.11.5.3 `modules/consumer-iam/outputs.tf`

```hcl
output "consumer_role_arn" { value = aws_iam_role.consumer.arn }
```

### 3.11.5.4 Apendear `module "consumer_iam"` en `infra/envs/prod/main.tf`

Pegar AL FINAL de `infra/envs/prod/main.tf` (despues del `module "cicd"`
de §3.11.4):

```hcl
# -------------------------------------------------------------------------
# Capa 10: Consumer IAM (Patch 13.5 — repo ml-serving consume artifacts read-only)
# -------------------------------------------------------------------------
module "consumer_iam" {
  source = "../../modules/consumer-iam"

  project              = var.project
  artifacts_bucket_arn = module.storage.artifacts_bucket_arn
  consumer_oidc_arn    = data.aws_iam_openid_connect_provider.github.arn
  consumer_org         = var.consumer_org
  consumer_repo        = var.consumer_repo
}
```

> **En consola AWS veras**:
> - IAM → Roles → `ml-training-consumer` con trust policy que confia en
>   el OIDC provider de §2.5 y limita el `sub` a
>   `repo:<consumer_org>/<consumer_repo>:*` (repo distinto al de training).
> - IAM → Roles → `ml-training-consumer` → Permissions: inline policy con
>   `s3:GetObject` + `s3:ListBucket` sobre el bucket de artifacts (scope
>   minimo: el repo consumer **solo lee** modelos, no entrena ni publica).
> - El ARN se exporta como output `consumer_role_arn` (§3.2.6) — ese
>   ARN va al repo consumer como `vars.AWS_CONSUMER_ROLE_ARN` para que
>   su workflow GHA pueda hacer `aws-actions/configure-aws-credentials`
>   contra este rol.

---

## 3.12 Verificacion sintactica antes de Parte 4

Terminaste de pegar 9 modulos + envs/prod (~2500 lineas de HCL). Antes
de mover a la Parte 4 (donde haces `terraform apply` real), valida la
sintaxis localmente. Esto cuesta ~30 segundos y atrapa typos antes
de que cuesten un apply de 18 min.

### 3.12.1 `terraform fmt` recursivo

```bash
# Normaliza espaciado en TODA la jerarquia infra/
terraform fmt -recursive infra/
# Si imprime nombres de archivos, los reformateo. Re-pegar es OK.
# Si no imprime nada, ya estaba todo formateado.

# Verificacion strict (sin reformatear, falla si algo esta mal)
terraform fmt -check -recursive infra/
echo "rc=$?"
# Esperado: rc=0
```

### 3.12.2 `terraform validate` por modulo

Cada modulo es un workspace independiente para Terraform (no hay
`backend`, solo recursos). Se valida sin tocar AWS:

```bash
for mod in network storage mlflow reports batch monitoring lambdas scheduler cicd; do
  echo "=== modules/${mod} ==="
  terraform -chdir="infra/modules/${mod}" init -backend=false -input=false > /dev/null
  terraform -chdir="infra/modules/${mod}" validate
done
# Esperado: cada modulo imprime "Success! The configuration is valid."
```

> **Por que `init -backend=false`**: los modulos no tienen backend
> (eso lo declara `envs/prod`). El init sin backend solo descarga
> providers para que validate pueda chequear tipos. No crea state ni
> toca AWS.

### 3.12.3 `terraform validate` del root (`envs/prod`)

```bash
# envs/prod si tiene backend. Init contra el backend real:
cd infra/envs/prod
terraform init -reconfigure  # usa backend.tf con S3 + DDB
terraform validate
cd ../../..
# Esperado: "Success! The configuration is valid."
```

> **Si algun modulo falla validate**: el error indica archivo y linea
> exacta. Re-pegar el bloque corregido y re-correr el loop. NO seguir
> a Parte 4 hasta que los 9 modulos + envs/prod den "Success".

> **Cierre Parte 3.**
>
> Estado actual: el repo tiene escritos `infra/envs/prod/` (6 archivos),
> 9 módulos en `infra/modules/`, 3 archivos Python en `infra/lambdas/`
> y 3 archivos en `docker/reports/`. Todo lockeado al contrato real del
> trainer (env vars S3, command `--varieties X --tuning Y`, variedades
> válidas, custom metric MAPE con dimension `variety`).
>
# Parte 4 — Apply incremental + smoke test

> **Filosofia de la Parte 4**: aplicar Terraform en 3 olas en vez de un
> `terraform apply` monolitico. Esto te da puntos de rollback claros y
> evita el dolor de "el apply de 25 min fallo en el ultimo recurso y
> ahora no se en que estado quedo todo".
>
> Las olas:
>
> | Ola | Modulos | Tiempo | Sirve para |
> |---|---|---|---|
> | A | `storage` (S3 + ECR) | ~1 min | Tener ECR donde pushear las imagenes |
> | B | (build + push trainer + mlflow + reports a ECR) | ~20-30 min | Imagenes disponibles para Fargate / Batch |
> | C | resto (`network`, `mlflow`, `reports`, `batch`, `monitoring`, `lambdas`, `scheduler`, `cicd`) | ~15-20 min | Infra operativa, ALB con health checks 200 |
>
> Si haces apply monolitico (Ola A+C juntas), Fargate intenta arrancar
> MLflow con la imagen que todavia no esta pusheada → bucle de retries
> hasta que el deployment timeout te mata.

## 4.1 Setup Task (orquestador local)

[Task](https://taskfile.dev) es un task runner moderno (binario Go single-file,
cross-platform). Orquesta los apply de Terraform + builds Docker + invocaciones
Lambda + jobs de Batch desde una **sola lista descubrible** (`task --list`).

**Por que Task y no Ansible (V1 patron deprecated):**

| Criterio | Ansible | Task |
|---|---|---|
| Soporte Windows | No nativo (requiere WSL Ubuntu + pipx + wrappers en `$PROFILE`). En WSL: pesado por dependencias Python | Single-binary 10 MB. Corre fluido en WSL Ubuntu (entorno usado por esta guia) y tambien nativo en Windows si hiciera falta |
| Dependencia | Python 3 + pipx + Jinja2 + ~200 MB | Single binary ~10 MB |
| Sintaxis | YAML + Jinja + DSL (`ansible.builtin.command`, `register`, `block:/rescue:`) | YAML limpio + POSIX shell |
| Cache de builds | No (siempre re-corre) | Si (`sources:` con hash) |
| Modularidad | `roles/` (4 archivos por role) | `includes:` directo |
| Confirmacion en destructivos | `pre_tasks` con `pause` (verboso) | `prompt:` field (1 linea) |

El stack es Docker + AWS managed services (containers stateless + APIs),
no hosts EC2 con software instalado. Ansible brillaba en "configurar 20
servidores"; aqui es overkill. Task encaja exactamente en el punto medio
entre Make (limitado) y Ansible (sobrado).

Cada task es **idempotente y composable**: si falla en el paso 5/10, lo
corres de nuevo y empieza desde donde quedo (Terraform mismo es idempotente;
los hash de `sources:` evitan re-buildear imagenes ya construidas).

### 4.1.1 Verificar / instalar Task

```bash
task --version
# Esperado: 3.34+ (necesario para `prompt:` en tasks destructivos)
```

Si falta (ya cubierto en Capítulo 3.1; recordatorio aqui):

```bash
# Windows (WSL Ubuntu) y Linux: mismo instalador
sh -c "$(curl --location https://taskfile.dev/install.sh)" -- -d -b ~/bin
export PATH="$HOME/bin:$PATH"   # agregar a ~/.bashrc para persistir

# macOS
brew install go-task
```

### 4.1.2 Estructura final

Despues de seguir §4.1.3 a §4.1.9 + §4.1.11, tu proyecto va a tener:

```
Taskfile.yml                    # raiz: tasks LOCALES (Docker) + includes AWS
tasks/
├── infra.yml                   # infra:*       terraform + bootstrap
├── ecr.yml                     # ecr:*         build + push 3 imagenes
├── batch.yml                   # batch:*       submit jobs + polling
├── cluster.yml                 # cluster:*     lifecycle scale up/down + teardown
├── mlflow_registry.yml         # mlflow-aws:*  promote con quality gate MAPE + A/B
├── aws.yml                     # aws:*         orquestadores high-level (deploy/wake/sleep/nuke)
├── local.yml                   # local:*       helpers dev local (ensure-buckets, bucket-name)
└── lib/
    └── batch_wait.sh           # helper bash compartido (polling de Batch jobs)
```

**Por que un Taskfile raiz + 7 archivos en `tasks/` y no uno solo**:

- **Namespacing**: cada `includes:` prefija con `<nombre>:`, asi `task build`
  (local Docker existente) NO choca con `task ecr:build` (AWS nuevo).
- **Blast radius**: tocar `tasks/batch.yml` no riesga romper `cluster.yml`.
- **Discoverability**: `task --list` los muestra agrupados por namespace.
- **Tamano manageable**: un Taskfile monolitico de 600 lineas se vuelve un
  dolor de revisar; 7 archivos de 80-200 lineas son trozos auto-contenidos.

### 4.1.3 Crear `tasks/` y anadir `PROJECT` al Taskfile raiz

> **Orden importa**: el `includes:` viene en §4.1.10 — DESPUES de crear
> los 7 archivos referenciados (§4.1.4 a §4.1.9 + §4.1.11). Si
> pegas el `includes:` ahora, `task --list` falla por "archivo no
> encontrado" hasta que llegues a 4.1.10.

```bash
mkdir -p tasks
```

Editar `Taskfile.yml` raiz para anadir `PROJECT` y `REGION` al bloque
`vars:` existente (esto SI se hace ahora — los Taskfiles que vas a crear
en §4.1.4-4.1.9 los van a leer, propagados via `includes:.vars:` en
§4.1.10):

```yaml
vars:
  # ... vars existentes (TUNING, VARIETIES, PARALLEL) ...
  PROJECT: "{{.PROJECT | default \"ml-training\"}}"           # NUEVO, usado por tasks AWS
  REGION:  "{{.AWS_DEFAULT_REGION | default \"us-east-1\"}}"  # NUEVO, usado por tasks AWS
```

**Por que `PROJECT` y `REGION` se centralizan en el root**: las tasks AWS
los usan como nombre base de todos los recursos (ECR repos, RDS instance,
Batch queues, Lambda function names) y para apuntar a la region correcta.
Definirlos una sola vez en el root permite override via CLI
(`task aws:deploy PROJECT=ml-training-staging`) sin tocar ningun archivo
hijo. La propagacion via `includes:.vars:` (§4.1.10) los hace visibles
en cada `tasks/*.yml` sin redeclaraciones — go-task aisla el scope de los
includes, asi sin esto los hijos no verian las vars del root.

### 4.1.4 `tasks/infra.yml` (Terraform wrapper + bootstrap)

Crear el archivo con este contenido:

```yaml
# =============================================================================
# tasks/infra.yml  -  Terraform wrapper + bootstrap del backend
# =============================================================================
# Incluido por Taskfile.yml raiz con namespace "infra:".
#
# USO TIPICO:
#   task infra:bootstrap                              UNA VEZ por cuenta+region
#   task infra:bootstrap-oidc                         UNA VEZ (rol GHA)
#   task infra:plan [TARGET=module.X]                 ver cambios
#   task infra:apply [TARGET=module.X]                aplicar (parcial o full)
#   task infra:output                                 outputs (alb_dns, ecr_urls, ...)
#   task infra:validate                               fmt -check + validate (pre-commit)
#   task infra:destroy                                DESTRUCTIVO: todo
#   task infra:destroy-target TARGET=module.X         DESTRUCTIVO: parcial
# =============================================================================

version: "3"

vars:
  TF_DIR:  '{{.TF_DIR | default "infra/envs/prod"}}'
  # Ultimos 6 chars del account ID (sufijo de buckets para evitar colisiones).
  # Resuelto una vez por invocacion del include.
  SUFFIX:
    sh: aws sts get-caller-identity --query Account --output text | tail -c 7

tasks:

  # ═══ Bootstrap (one-shot, idempotente) ══════════════════════════════════════

  bootstrap:
    desc: "Backend Terraform (S3 + DynamoDB lock + SLRs). UNA VEZ por cuenta+region"
    cmds:
      - bash infra/bootstrap.sh

  bootstrap-oidc:
    desc: "Rol IAM para GitHub Actions via OIDC. UNA VEZ"
    cmds:
      - bash infra/bootstrap-oidc.sh

  # ═══ Init (interno, dep de plan/apply/destroy) ══════════════════════════════

  _init:
    internal: true
    cmds:
      - terraform -chdir={{.TF_DIR}} init
        -backend-config=bucket={{.PROJECT}}-tfstate-{{.SUFFIX}}
        -backend-config=key=envs/prod/terraform.tfstate
        -backend-config=region={{.REGION}}
        -backend-config=dynamodb_table={{.PROJECT}}-tflock
        -reconfigure

  # ═══ Plan / Apply / Destroy ═════════════════════════════════════════════════

  plan:
    desc: "terraform plan. Var opcional: TARGET=module.X"
    deps: [_init]
    cmds:
      - terraform -chdir={{.TF_DIR}} plan {{if .TARGET}}-target={{.TARGET}}{{end}}

  apply:
    desc: "terraform apply -auto-approve. Var opcional: TARGET=module.X (apply parcial por oleadas)"
    deps: [_init]
    cmds:
      - terraform -chdir={{.TF_DIR}} apply {{if .TARGET}}-target={{.TARGET}}{{end}} -auto-approve

  destroy:
    desc: "DESTRUCTIVO: terraform destroy completo. Considerar cluster:teardown antes (preserva storage)"
    prompt: "Esto borrara TODA la infra de envs/prod (incluso S3 + ECR). Continuar?"
    deps: [_init]
    cmds:
      - terraform -chdir={{.TF_DIR}} destroy -auto-approve

  destroy-target:
    desc: "terraform destroy parcial. Vars: TARGET=module.X (REQ)"
    prompt: "Destruir {{.TARGET}}? Asegurate que no tenga dependencias activas"
    requires:
      vars: [TARGET]
    deps: [_init]
    cmds:
      - terraform -chdir={{.TF_DIR}} destroy -target={{.TARGET}} -auto-approve

  # ═══ Inspeccion ═════════════════════════════════════════════════════════════

  output:
    desc: "Mostrar outputs de envs/prod (alb_dns, ecr_urls, rds_endpoint, ...)"
    cmds:
      - terraform -chdir={{.TF_DIR}} output

  output-raw:
    desc: "Mostrar UN output crudo (para scripts). Var: NAME=alb_dns (REQ)"
    silent: true
    requires:
      vars: [NAME]
    cmds:
      - terraform -chdir={{.TF_DIR}} output -raw {{.NAME}}

  validate:
    desc: "terraform fmt -check + validate (sin tocar state, util en pre-commit)"
    cmds:
      - terraform -chdir={{.TF_DIR}} fmt -check -recursive
      - terraform -chdir={{.TF_DIR}} validate

  # ═══ Recovery ═══════════════════════════════════════════════════════════════

  force-unlock:
    desc: "Liberar state lock huerfano. Var: LOCK_ID=<id> (REQ)"
    requires:
      vars: [LOCK_ID]
    deps: [_init]
    cmds:
      - terraform -chdir={{.TF_DIR}} force-unlock -force {{.LOCK_ID}}
```

**Por que `_init` es task interna y no expuesta**: el init no se invoca a
mano salvo recovery de backend movido. Para todos los flujos normales,
plan/apply/destroy lo disparan via `deps:`. Esconder lo que no debe
invocarse manualmente reduce ruido en `task --list`.

**Por que el `_init` resuelve `SUFFIX` cada vez (no lo cachea)**: cambia
entre cuentas AWS (sandbox vs prod). Resolverlo dinamicamente con
`aws sts get-caller-identity` evita que apliques a la cuenta equivocada
por error de configuracion estale.

**Por que `prompt:` en `destroy` y `destroy-target`**: son operaciones
irreversibles. Task pausa pidiendo `y` antes de ejecutar. Sin esto, un
typo en un workflow tira tu storage. La V1 con Ansible necesitaba
`vars_prompt` + `pre_tasks` con `assert`; aca es una sola linea.

**Por que `bootstrap` y `bootstrap-oidc` envuelven scripts bash existentes
y no replicann su logica**: los scripts ya son idempotentes y bien
probados. Envolverlos como tasks da consistencia (`task infra:bootstrap`
en vez de `bash infra/bootstrap.sh`) sin re-implementar.

### 4.1.5 `tasks/ecr.yml` (build + push 3 imagenes)

```yaml
# =============================================================================
# tasks/ecr.yml  -  Build + push de las 3 imagenes a ECR
# =============================================================================
# Incluido por Taskfile.yml raiz con namespace "ecr:".
#
# USO TIPICO:
#   task ecr:build-all                                build + push de las 3
#   task ecr:build IMG=trainer                        UNA imagen, tag default
#   task ecr:build IMG=trainer TAG=v1.2.3             UNA imagen, tag custom
#   task ecr:list                                     listar tags en ECR
#
# IMG = trainer | mlflow | reports
# =============================================================================

version: "3"

vars:
  TAG_TRAINER: '{{.TAG_TRAINER | default "latest"}}'
  TAG_MLFLOW:  '{{.TAG_MLFLOW  | default "v3.12.0"}}'
  TAG_REPORTS: '{{.TAG_REPORTS | default "stable"}}'

tasks:

  # ═══ Login (token 12h, run: once) ═══════════════════════════════════════════

  login:
    desc: "docker login a ECR. Idempotente, token valido 12h"
    # run: once: si varias tasks dependen de login en una misma corrida,
    # solo se ejecuta una vez.
    run: once
    vars:
      ACCOUNT:
        sh: aws sts get-caller-identity --query Account --output text
    cmds:
      - aws ecr get-login-password --region {{.REGION}}
        | docker login --username AWS --password-stdin {{.ACCOUNT}}.dkr.ecr.{{.REGION}}.amazonaws.com

  # ═══ Build + push UNA imagen ════════════════════════════════════════════════

  build:
    desc: "Build + push UNA imagen. Vars: IMG=trainer|mlflow|reports (REQ), TAG=<override>"
    requires:
      vars: [IMG]
    deps: [login]
    vars:
      ACCOUNT:
        sh: aws sts get-caller-identity --query Account --output text
      REGISTRY: '{{.ACCOUNT}}.dkr.ecr.{{.REGION}}.amazonaws.com'
      GIT_SHA:
        sh: git rev-parse --short=12 HEAD 2>/dev/null || echo unknown
      BUILD_DATE:
        sh: date -u +%Y-%m-%dT%H:%M:%SZ
      # Tabla IMG -> (image_name, dockerfile, default_tag). Cualquier IMG fuera
      # de trainer|mlflow|reports cae en el branch ERROR validado abajo.
      IMAGE_NAME: '{{if eq .IMG "trainer"}}{{.PROJECT}}{{else if eq .IMG "mlflow"}}{{.PROJECT}}-mlflow{{else if eq .IMG "reports"}}{{.PROJECT}}-reports{{else}}ERROR{{end}}'
      DOCKERFILE: '{{if eq .IMG "trainer"}}Dockerfile{{else if eq .IMG "mlflow"}}docker/mlflow/Dockerfile{{else if eq .IMG "reports"}}docker/reports/Dockerfile{{else}}ERROR{{end}}'
      RESOLVED_TAG: '{{if eq .IMG "trainer"}}{{.TAG | default .TAG_TRAINER}}{{else if eq .IMG "mlflow"}}{{.TAG | default .TAG_MLFLOW}}{{else if eq .IMG "reports"}}{{.TAG | default .TAG_REPORTS}}{{else}}ERROR{{end}}'
    cmds:
      - 'test "{{.IMAGE_NAME}}" != "ERROR" || { echo "ERROR IMG debe ser trainer|mlflow|reports (recibido {{.IMG}})"; exit 1; }'
      - 'echo ">>> Build {{.IMAGE_NAME}}:{{.RESOLVED_TAG}}  (sha-{{.GIT_SHA}})"'
      - docker build
        --build-arg GIT_SHA={{.GIT_SHA}}
        --build-arg BUILD_DATE={{.BUILD_DATE}}
        --build-arg VERSION={{.RESOLVED_TAG}}
        -t {{.REGISTRY}}/{{.IMAGE_NAME}}:{{.RESOLVED_TAG}}
        -t {{.REGISTRY}}/{{.IMAGE_NAME}}:sha-{{.GIT_SHA}}
        -f {{.DOCKERFILE}} .
      - docker push {{.REGISTRY}}/{{.IMAGE_NAME}}:{{.RESOLVED_TAG}}
      - docker push {{.REGISTRY}}/{{.IMAGE_NAME}}:sha-{{.GIT_SHA}}

  # ═══ Build + push de las 3 ══════════════════════════════════════════════════

  build-all:
    desc: "Build + push de las 3 imagenes (trainer + mlflow + reports)"
    deps: [login]
    cmds:
      - task: build
        vars: { IMG: trainer }
      - task: build
        vars: { IMG: mlflow }
      - task: build
        vars: { IMG: reports }

  # ═══ Inspeccion ═════════════════════════════════════════════════════════════

  list:
    desc: "Listar las 3 imagenes con tag default presente en cada repo ECR"
    silent: true
    cmds:
      - |
        for spec in "{{.PROJECT}}:{{.TAG_TRAINER}}" "{{.PROJECT}}-mlflow:{{.TAG_MLFLOW}}" "{{.PROJECT}}-reports:{{.TAG_REPORTS}}"; do
          repo="${spec%:*}"; tag="${spec##*:}"
          echo "=== $repo ($tag) ==="
          aws ecr list-images --repository-name "$repo" \
            --query "imageIds[?imageTag=='$tag']" --output table 2>/dev/null || true
          echo ""
        done
```

**Por que `run: once` en `login`**: cuando `build-all` lanza las 3 builds
en secuencia, cada una declara `deps: [login]`. Sin `run: once`, harias
3 `aws ecr get-login-password` seguidos. Con `run: once`, login corre una
vez y las 3 builds reusan el token.

**Por que 2 tags por imagen** (`latest`/`v3.12.0`/`stable` + `sha-<git-sha>`):
- El movil (`latest`) sirve a CI/CD continuo donde "el ultimo build" es
  lo que importa.
- El `sha-<git-sha>` da rollback determinista: si una vuelta de produccion
  falla, podes hacer `task ecr:build IMG=trainer TAG=sha-<commit-anterior>`
  para volver exacto.

**Por que templating go-task con `{{if eq .IMG ...}}` y no `case` en bash**:
en Task, `vars` se evaluan al cargar la task. Usar el motor de templates
nativo (en vez de `sh: | case ...` por var) evita 3 fork de subshell, es
mas portable (cero dependencias en bash POSIX) y deja la tabla
IMG -> (image_name, dockerfile, default_tag) como un one-liner por var.
La validacion `test "{{.IMAGE_NAME}}" != "ERROR"` falla rapido si IMG
viene mal escrito y permite parametrizar sin escribir 3 tasks separadas
(`build:trainer`, `build:mlflow`, `build:reports`).

**Por que pasamos `BUILD_DATE` como build-arg**: el Dockerfile lo recibe
y lo embebe como label (`org.opencontainers.image.created`). Util para
auditar "que imagen tengo desplegada y desde cuando" via
`docker inspect <image>`.

### 4.1.6 `tasks/batch.yml` (training jobs en la nube)

```yaml
# =============================================================================
# tasks/batch.yml  -  AWS Batch (training jobs en la nube)
# =============================================================================
# Incluido por Taskfile.yml raiz con namespace "batch:".
#
# USO TIPICO:
#   task batch:train VARIETIES=POP                    una variedad, espera
#   task batch:train VARIETIES=POP,VENTURA            multiples en serie
#   task batch:train VARIETIES=POP TUNING=smoke       sanity check (~1 min)
#   task batch:train VARIETIES=POP WAIT=false         fire-and-forget
#   task batch:train-lambda VARIETIES=POP                       submit via Lambda dispatcher (valida variety + S3 hydrate)
#   task batch:smoke                                  atajo: POP + smoke
#   task batch:status                                 jobs activos en queues
#   task batch:cancel JOB_ID=<id>                     terminar un job
# =============================================================================

version: "3"

vars:
  JOB_DEF:    '{{.JOB_DEF    | default (printf "%s-trainer"  .PROJECT)}}'
  QUEUE_SPOT: '{{.QUEUE_SPOT | default (printf "%s-job-queue-spot"     .PROJECT)}}'
  QUEUE_OD:   '{{.QUEUE_OD   | default (printf "%s-job-queue-ondemand" .PROJECT)}}'
  TUNING:     '{{.TUNING     | default "prod"}}'
  PARALLEL:   '{{.PARALLEL   | default "1"}}'
  WAIT:       '{{.WAIT       | default "true"}}'

tasks:

  # ═══ Entrenar ═══════════════════════════════════════════════════════════════

  train:
    desc: "Entrena en Batch (1 o N variedades, secuencial). Vars: VARIETIES=POP[,JUPITER] (REQ), TUNING, WAIT"
    requires:
      vars: [VARIETIES]
    vars:
      # prod_xl -> OnDemand (sin interrupcion). Resto -> Spot (~70% cheaper).
      QUEUE: '{{if eq .TUNING "prod_xl"}}{{.QUEUE_OD}}{{else}}{{.QUEUE_SPOT}}{{end}}'
    cmds:
      - |
        set -e
        source tasks/lib/batch_wait.sh
        for v in $(echo "{{.VARIETIES}}" | tr ',' ' '); do
          OVERRIDES=$(jq -nc \
            --arg v "$v" --arg t "{{.TUNING}}" --arg p "{{.PARALLEL}}" \
            '{command: ["--varieties", $v, "--tuning", $t, "--parallel-varieties", $p]}')
          JOB_ID=$(aws batch submit-job \
            --job-name       "train-$v-{{.TUNING}}-$(date +%Y%m%d-%H%M%S)" \
            --job-queue      "{{.QUEUE}}" \
            --job-definition "{{.JOB_DEF}}" \
            --container-overrides "$OVERRIDES" \
            --query jobId --output text)
          echo ">>> $v  job=$JOB_ID  queue={{.QUEUE}}"
          [ "{{.WAIT}}" != "true" ] && continue
          wait_job "$JOB_ID" "$v"
        done

  # ═══ Entrenar via Lambda dispatcher (path usado por train.yml) ═════════════

  train-lambda:
    desc: "Submit via Lambda dispatcher (valida variety + S3 hydrate). Vars: VARIETIES (REQ), TUNING, WAIT"
    requires:
      vars: [VARIETIES]
    vars:
      DISPATCHER_FN: '{{.DISPATCHER_FN | default (printf "%s-dispatcher" .PROJECT)}}'
    cmds:
      - |
        set -e
        source tasks/lib/batch_wait.sh
        PAYLOAD=$(jq -nc --arg v "{{.VARIETIES}}" --arg t "{{.TUNING}}" \
          '{varieties: $v, tuning: $t}')
        aws lambda invoke \
          --function-name "{{.DISPATCHER_FN}}" \
          --cli-binary-format raw-in-base64-out \
          --payload "$PAYLOAD" \
          /tmp/dispatcher-out.json \
          --query 'StatusCode' --output text
        cat /tmp/dispatcher-out.json
        JOB_ID=$(jq -r '.body.jobId // (.body|fromjson|.jobId)' /tmp/dispatcher-out.json 2>/dev/null || jq -r '.jobId' /tmp/dispatcher-out.json)
        echo ">>> Submitted via dispatcher  job=$JOB_ID"
        [ "{{.WAIT}}" != "true" ] && exit 0
        wait_job "$JOB_ID" "dispatcher"

  # ═══ Smoke test ═════════════════════════════════════════════════════════════

  smoke:
    desc: "Sanity check end-to-end (~1 min). Equivalente a train VARIETIES=POP TUNING=smoke"
    cmds:
      - task: train
        vars: { VARIETIES: POP, TUNING: smoke }

  # ═══ Estado ═════════════════════════════════════════════════════════════════

  status:
    desc: "Jobs activos (SUBMITTED/PENDING/RUNNABLE/STARTING/RUNNING) en ambas queues"
    silent: true
    cmds:
      - |
        for queue in "{{.QUEUE_SPOT}}" "{{.QUEUE_OD}}"; do
          echo "=== $queue ==="
          for s in SUBMITTED PENDING RUNNABLE STARTING RUNNING; do
            aws batch list-jobs --job-queue "$queue" --job-status $s \
              --query 'jobSummaryList[].[jobId,jobName,status,createdAt]' --output table 2>/dev/null || true
          done
          echo ""
        done

  # ═══ Cancelar ═══════════════════════════════════════════════════════════════

  cancel:
    desc: "Terminar job RUNNING/PENDING. Vars: JOB_ID=<id> (REQ), REASON=<texto>"
    requires:
      vars: [JOB_ID]
    cmds:
      - aws batch terminate-job --job-id "{{.JOB_ID}}" --reason "{{.REASON | default \"cancelled via task\"}}"
```

**Por que la queue cambia con `TUNING`**: `prod_xl` corre 5-6h y la
probabilidad de kill Spot a esa duracion es 20-30%. Para tunings mas
cortos, Spot ahorra ~70% del costo y el retry=2 del job-def cubre
interrupciones. La logica `{{if eq .TUNING "prod_xl"}}...{{else}}...{{end}}`
selecciona automaticamente.

**Por que polling cada 30s**: `describe-jobs` cuenta contra el API
rate-limit de Batch. 30s da resolucion suficiente (los jobs duran
20m-6h) sin saturar.

**Por que `train` colapsa submit + polling + multi-variedad en una sola
task** (en vez de `submit` + `wait` + `retrain` separados como en V1
del runbook):
- El uso comun es "lanza y espera"; tenerlo en un solo loop bash es mas
  legible que dos tasks que se invocan recursivamente.
- Multi-variedad es solo un `for v in $(echo "$VARIETIES" | tr ',' ' ')`
  alrededor del bloque submit+wait. No justifica una task aparte.
- Si necesitas fire-and-forget, `WAIT=false` saltea el polling.
- Si necesitas observar un job lanzado por otra via (GitHub Actions,
  consola AWS), usa `aws batch describe-jobs --jobs <id>` directo.

**Por que iteramos con bash `for` y no con paralelismo Task**: los jobs
Batch ya corren en paralelo en infraestructura AWS. El loop local solo
necesita lanzarlos secuencialmente para que el polling sea ordenado en
la terminal. Si queres todo paralelo, `WAIT=false`.

### 4.1.6.1 `tasks/lib/batch_wait.sh` — Helper compartido de polling

El bloque de polling (`describe-jobs` cada 30s con `SUCCEEDED`/`FAILED`)
se extrajo a un helper bash sourceado para evitar duplicacion entre
`batch:train` y `batch:train-lambda`. Ambas tasks hacen
`source tasks/lib/batch_wait.sh` al inicio del comando y luego invocan
`wait_job <job_id> <label>` (donde `<label>` es la variedad o `dispatcher`,
solo para prefijar el log).

```bash
# Helpers bash compartidos por tasks/batch.yml (train / train-lambda).
# Sourceado, no ejecutado. Requiere awscli configurado.

# wait_job <job_id> <label>
# Polling cada 30s hasta SUCCEEDED (return 0) o FAILED (return 1).
wait_job() {
  local job_id="$1" label="$2"
  while :; do
    local status
    status=$(aws batch describe-jobs --jobs "$job_id" --query 'jobs[0].status' --output text)
    echo "  $(date +%H:%M:%S)  $label  $status"
    case "$status" in
      SUCCEEDED) return 0 ;;
      FAILED)
        local reason
        reason=$(aws batch describe-jobs --jobs "$job_id" --query 'jobs[0].statusReason' --output text)
        echo "FAIL $label  reason=$reason"
        return 1
        ;;
      *) sleep 30 ;;
    esac
  done
}
```

### 4.1.7 `tasks/cluster.yml` (lifecycle scale up/down + teardown)

```yaml
# =============================================================================
# tasks/cluster.yml  -  Lifecycle del cluster AWS
# =============================================================================
# Incluido por Taskfile.yml raiz con namespace "cluster:".
#
# Modulos VOLATILES (se reconstruyen en ~10-15 min):
#   scheduler, lambdas, monitoring, batch, reports, mlflow
# Modulos PERMANENTES (NO se tocan en teardown):
#   network (VPC + NAT $$$), storage (S3 + ECR), backend state
#
# USO TIPICO:
#   task cluster:status                               estado RDS + ECS + Batch
#   task cluster:scale-up                             encender (RDS + Fargate)
#   task cluster:scale-down                           apagar (preserva infra)
#   task cluster:wait-healthy                         polling ALB hasta 200
#   task cluster:teardown                             destroy volatiles
#   task cluster:rebuild                              re-apply + scale-up
# =============================================================================

version: "3"

vars:
  SCHEDULER_FN: '{{.SCHEDULER_FN | default (printf "%s-scheduler" .PROJECT)}}'
  TF_DIR:       '{{.TF_DIR       | default "infra/envs/prod"}}'
  QUEUE_SPOT:   '{{.QUEUE_SPOT   | default (printf "%s-job-queue-spot"     .PROJECT)}}'
  QUEUE_OD:     '{{.QUEUE_OD     | default (printf "%s-job-queue-ondemand" .PROJECT)}}'
  # Orden reverso de apply (importante para destroy con dependencias)
  VOLATILE_MODULES: "module.scheduler module.lambdas module.monitoring module.batch module.reports module.mlflow"

tasks:

  # ═══ Estado ═════════════════════════════════════════════════════════════════

  status:
    desc: "Estado del cluster: RDS + ECS services + Batch jobs activos"
    silent: true
    cmds:
      - 'echo "=== RDS ==="'
      - aws rds describe-db-instances --db-instance-identifier {{.PROJECT}}-mlflow
        --query 'DBInstances[0].[DBInstanceStatus,DBInstanceClass,Endpoint.Address]'
        --output table 2>/dev/null || echo "  (RDS no existe o no accesible)"
      - 'echo ""'
      - 'echo "=== ECS Services ==="'
      - aws ecs describe-services --cluster {{.PROJECT}}-cluster
        --services {{.PROJECT}}-mlflow {{.PROJECT}}-reports
        --query 'services[].[serviceName,desiredCount,runningCount,pendingCount]'
        --output table 2>/dev/null || echo "  (ECS no existe o servicios no creados)"
      - 'echo ""'
      - 'echo "=== Batch jobs activos ==="'
      - task: _batch-jobs-active

  _batch-jobs-active:
    internal: true
    silent: true
    cmds:
      - |
        total=0
        for q in {{.QUEUE_SPOT}} {{.QUEUE_OD}}; do
          for s in SUBMITTED PENDING RUNNABLE STARTING RUNNING; do
            n=$(aws batch list-jobs --job-queue "$q" --job-status $s --query 'length(jobSummaryList)' --output text 2>/dev/null || echo 0)
            [ "$n" -gt 0 ] && { echo "  queue=$q  status=$s  count=$n"; total=$((total + n)); }
          done
        done
        echo "  TOTAL activos $total"

  _assert-no-running:
    internal: true
    silent: true
    cmds:
      - |
        running=0
        for q in {{.QUEUE_SPOT}} {{.QUEUE_OD}}; do
          n=$(aws batch list-jobs --job-queue "$q" --job-status RUNNING --query 'length(jobSummaryList)' --output text 2>/dev/null || echo 0)
          running=$((running + n))
        done
        if [ "$running" -gt 0 ]; then
          echo "ERROR $running job(s) RUNNING. Cancelar primero:"
          echo "       task batch:status              (ver detalle)"
          echo "       task batch:cancel JOB_ID=<id>  (cancelar)"
          exit 1
        fi

  # ═══ Scale (encender / apagar) ══════════════════════════════════════════════

  scale-down:
    desc: "Apagar (RDS stop + ECS desired=0). Aborta si hay Batch RUNNING"
    cmds:
      - 'echo ">>> Pre-check Batch jobs activos"'
      - task: _batch-jobs-active
      - task: _assert-no-running
      - 'echo ">>> Invocando scheduler Lambda (action=stop)..."'
      - aws lambda invoke --function-name {{.SCHEDULER_FN}}
        --payload '{"action":"stop"}'
        --cli-binary-format raw-in-base64-out
        /tmp/scheduler-stop.json
      - cat /tmp/scheduler-stop.json && echo ""

  scale-up:
    desc: "Encender (RDS start + ECS desired=1). RDS tarda ~5 min en estar Available"
    cmds:
      - 'echo ">>> Invocando scheduler Lambda (action=start)..."'
      - aws lambda invoke --function-name {{.SCHEDULER_FN}}
        --payload '{"action":"start"}'
        --cli-binary-format raw-in-base64-out
        /tmp/scheduler-start.json
      - cat /tmp/scheduler-start.json && echo ""
      - 'echo ""'
      - 'echo "RDS tarda ~5 min. Despues: task cluster:wait-healthy"'

  # ═══ Wait healthy ═══════════════════════════════════════════════════════════

  wait-healthy:
    desc: "Polling del ALB hasta MLflow 200. Timeout 10 min"
    deps: [_init-alb]
    cmds:
      - |
        ALB=$(terraform -chdir={{.TF_DIR}} output -raw alb_dns 2>/dev/null)
        if [ -z "$ALB" ]; then
          echo "ERROR no se pudo leer alb_dns. envs/prod aplicada?"
          exit 1
        fi
        echo "Polling http://$ALB/ cada 15s (timeout 10 min)..."
        for i in $(seq 1 40); do
          code=$(curl -s -o /dev/null -w '%{http_code}' "http://$ALB/" 2>/dev/null || echo 000)
          echo "  $(date +%H:%M:%S)  GET http://$ALB/  -> $code"
          [ "$code" = "200" ] && { echo "OK MLflow respondiendo en http://$ALB/"; exit 0; }
          sleep 15
        done
        echo "FAIL timeout 10 min. Revisar: aws logs tail /ecs/{{.PROJECT}}/mlflow --follow"
        exit 1

  _init-alb:
    internal: true
    cmds:
      - 'test -d {{.TF_DIR}} || { echo "ERROR {{.TF_DIR}} no existe. Aplicar infra: task infra:apply"; exit 1; }'

  # ═══ Teardown / Rebuild ═════════════════════════════════════════════════════

  teardown:
    desc: "scale-down + destroy modulos volatiles. Preserva storage + network"
    prompt: "Destruira los modulos volatiles. Storage (S3+ECR) y network (VPC) quedan. Continuar?"
    cmds:
      - task: scale-down
      - 'echo ">>> Destroy modulos volatiles (orden reverso de apply)..."'
      - |
        for mod in {{.VOLATILE_MODULES}}; do
          echo ">>> terraform destroy -target=$mod"
          terraform -chdir={{.TF_DIR}} destroy -target=$mod -auto-approve || {
            echo "FAIL destroy de $mod fallo. Revisar manualmente."
            exit 1
          }
        done
      - 'echo "OK teardown completo. Para volver: task cluster:rebuild"'

  rebuild:
    desc: "Re-apply de modulos volatiles + scale-up"
    cmds:
      - 'echo ">>> Apply completo (modulos volatiles se re-crean, resto no-op)..."'
      - task: ":infra:apply"
      - 'echo ">>> scale-up..."'
      - task: scale-up
      - 'echo ""'
      - 'echo "Listo. task cluster:wait-healthy para confirmar MLflow"'
```

**Por que invocar el Lambda scheduler y no `aws cli` directo desde la
task**: la logica (drenar Batch -> apagar Fargate -> stop RDS en orden,
mas chequeos y notificaciones SNS) ya vive en `infra/lambdas/scheduler.py`.
Re-implementarla en bash duplicaria mantenimiento y deriva con el tiempo.

**Por que el pre-check explicito de Batch jobs antes de scale-down (y no
delegado al scheduler)**: el scheduler tambien chequea, pero la task
muestra el error en la terminal del operador con sugerencias accionables
(`task batch:cancel JOB_ID=<id>`). El helper interno `_assert-no-running`
itera ambas queues (Spot + OnDemand) y aborta con codigo 1; si solo
confiaras en el Lambda, el operador veria un payload JSON con
`error: jobs running` y tendria que buscar como cancelarlos.

**Por que `teardown` preserva `module.network` y storage**: el NAT
Gateway dentro de network cuesta $32/mes encendido pero su create
tarda 5+ min cada vuelta. Si vas a teardown frecuentemente (>1 vez/mes),
preservar el NAT es net-positivo. Si vas a hibernar largo (>3 meses),
incluilo manualmente: `task infra:destroy-target TARGET=module.network`.

**Por que `rebuild` hace `task infra:apply` (full) y no solo los modulos
volatiles**: Terraform es idempotente. Los modulos no destruidos quedan
no-op. Mas simple que mantener una lista paralela de "modulos a re-apply".

**Por que `wait-healthy` usa `terraform output -raw alb_dns` cada vez y
no cachea**: el ALB DNS puede cambiar tras un teardown/rebuild. Resolver
dinamicamente garantiza que poll-eamos el ALB actual, no uno fantasma.

### 4.1.8 `tasks/mlflow_registry.yml` (promote con quality gate MAPE)

```yaml
# =============================================================================
# tasks/mlflow_registry.yml  -  MLflow Model Registry (AWS)
# =============================================================================
# Incluido por Taskfile.yml raiz con namespace "mlflow-aws:".
# Separado del MLflow local (que vive en docker-compose).
#
# USO TIPICO:
#   task mlflow-aws:list-versions MODEL_NAME=rnd-forest-POP                    ver versiones
#   task mlflow-aws:current-prod  MODEL_NAME=rnd-forest-POP                    ver Production actual
#   task mlflow-aws:promote MODEL_NAME=rnd-forest-POP VERSION=3                gate MAPE<=20 (default)
#   task mlflow-aws:promote MODEL_NAME=rnd-forest-POP VERSION=3 MAX_MAPE=15    gate custom
# =============================================================================

version: "3"

vars:
  TF_DIR:   '{{.TF_DIR   | default "infra/envs/prod"}}'
  MAX_MAPE: '{{.MAX_MAPE | default "20"}}'

tasks:

  # ═══ Helper: resolver MLflow URI ════════════════════════════════════════════

  _mlflow-uri:
    internal: true
    silent: true
    cmds:
      - |
        # Prioridad: env var MLFLOW_ALB_DNS (usada por GHA via vars.MLFLOW_ALB_DNS)
        # -> fallback: terraform output (uso local, requiere init previo)
        if [ -n "${MLFLOW_ALB_DNS:-}" ]; then
          echo "http://$MLFLOW_ALB_DNS"
          exit 0
        fi
        ALB=$(terraform -chdir={{.TF_DIR}} output -raw alb_dns 2>/dev/null)
        if [ -z "$ALB" ]; then
          echo "ERROR no se pudo leer alb_dns (ni env MLFLOW_ALB_DNS ni terraform output)" >&2
          exit 1
        fi
        echo "http://$ALB"

  # ═══ Inspeccion ═════════════════════════════════════════════════════════════

  list-versions:
    desc: "Listar versiones de un modelo. Var: MODEL_NAME=rnd-forest-POP (REQ)"
    requires:
      vars: [MODEL_NAME]
    cmds:
      - |
        URI=$(task mlflow-aws:_mlflow-uri)
        curl -s "$URI/api/2.0/mlflow/registered-models/get?name={{.MODEL_NAME}}" \
          | jq '.registered_model.latest_versions[] | {version, current_stage, run_id, creation_timestamp}'

  current-prod:
    desc: "Mostrar version Production actual. Var: MODEL_NAME=rnd-forest-POP (REQ)"
    requires:
      vars: [MODEL_NAME]
    cmds:
      - |
        URI=$(task mlflow-aws:_mlflow-uri)
        curl -s "$URI/api/2.0/mlflow/registered-models/get?name={{.MODEL_NAME}}" \
          | jq '.registered_model.latest_versions[] | select(.current_stage == "Production") | {version, run_id, creation_timestamp}'

  # ═══ Promote con quality gate (MAPE) ════════════════════════════════════════

  promote:
    desc: "Promover a Production con gate MAPE + A/B contra Production actual. Vars: MODEL_NAME (REQ, ej rnd-forest-POP), VERSION=N (REQ), MAX_MAPE=20"
    requires:
      vars: [MODEL_NAME, VERSION]
    cmds:
      - |
        URI=$(task mlflow-aws:_mlflow-uri)
        echo ">>> {{.MODEL_NAME}} v{{.VERSION}}  (gate MAPE <= {{.MAX_MAPE}})"

        # ── 1) Gate absoluto: MAPE del candidato <= MAX_MAPE ──────────────────
        RUN_ID=$(curl -s "$URI/api/2.0/mlflow/model-versions/get?name={{.MODEL_NAME}}&version={{.VERSION}}" \
          | jq -r '.model_version.run_id')
        if [ -z "$RUN_ID" ] || [ "$RUN_ID" = "null" ]; then
          echo "ERROR no se encontro {{.MODEL_NAME}} v{{.VERSION}}"; exit 1
        fi
        MAPE_NEW=$(curl -s "$URI/api/2.0/mlflow/runs/get?run_id=$RUN_ID" \
          | jq -r '.run.data.metrics[] | select(.key == "mape_oof" or .key == "mape") | .value' | head -n1)
        if [ -z "$MAPE_NEW" ] || [ "$MAPE_NEW" = "null" ]; then
          echo "ERROR el run no tiene mape_oof ni mape"; exit 1
        fi
        echo "    candidato mape=$MAPE_NEW"
        OK=$(awk -v m="$MAPE_NEW" -v t="{{.MAX_MAPE}}" 'BEGIN { print (m <= t) ? "yes" : "no" }')
        if [ "$OK" != "yes" ]; then
          echo "GATE FAIL absoluto  MAPE=$MAPE_NEW > {{.MAX_MAPE}}"; exit 1
        fi
        echo "GATE absoluto OK"

        # ── 2) Gate A/B: candidato debe mejorar al Production actual ──────────
        PROD_VER=$(curl -s "$URI/api/2.0/mlflow/registered-models/get-latest-versions" \
          -H "Content-Type: application/json" \
          -d "{\"name\":\"{{.MODEL_NAME}}\",\"stages\":[\"Production\"]}" \
          | jq -r '.model_versions[0].version // empty')
        if [ -z "$PROD_VER" ]; then
          echo "Sin Production previo, skip A/B"
        else
          PROD_RUN=$(curl -s "$URI/api/2.0/mlflow/model-versions/get?name={{.MODEL_NAME}}&version=$PROD_VER" \
            | jq -r '.model_version.run_id')
          MAPE_PROD=$(curl -s "$URI/api/2.0/mlflow/runs/get?run_id=$PROD_RUN" \
            | jq -r '.run.data.metrics[] | select(.key == "mape_oof" or .key == "mape") | .value' | head -n1)
          echo "    Production v$PROD_VER mape=$MAPE_PROD"
          BETTER=$(awk -v n="$MAPE_NEW" -v p="$MAPE_PROD" 'BEGIN { print (n < p) ? "yes" : "no" }')
          if [ "$BETTER" != "yes" ]; then
            echo "GATE FAIL A/B  candidato MAPE=$MAPE_NEW no mejora vs Production v$PROD_VER MAPE=$MAPE_PROD"; exit 1
          fi
          echo "GATE A/B OK"
        fi

        # ── 3) Transition ────────────────────────────────────────────────────
        echo ">>> Transicionando a Production (archive existing)..."
        curl -s -X POST "$URI/api/2.0/mlflow/model-versions/transition-stage" \
          -H "Content-Type: application/json" \
          -d "{\"name\":\"{{.MODEL_NAME}}\",\"version\":\"{{.VERSION}}\",\"stage\":\"Production\",\"archive_existing_versions\":true}" \
          | jq '.model_version | {name, version, current_stage}'
        echo "OK {{.MODEL_NAME}} v{{.VERSION}} en Production"
```

**Por que via REST API (`curl + jq`) y no `mlflow` CLI**: el host (Windows
+ WSL Ubuntu, o Linux/Mac) no necesariamente tiene `mlflow` CLI instalado,
y agregar Python + mlflow al host duplica la dependencia que ya vive en
el container del trainer. `curl + jq` son ubicuos y la API REST de MLflow
es estable cross-version (2.x y 3.x).

**Por que `awk` para comparar floats**: bash no soporta comparacion de
floats nativamente (`[ 19.5 -le 20 ]` falla con "integer expression
expected"). `awk` lo hace en una linea.

**Por que el gate prefiere `mape_oof` y cae a `mape`**: `mape_oof` es la
metric out-of-fold (validacion cross-validation), no la del train set.
La metric del train set siempre se ve bien (overfit); `oof` es lo que
predice generalizacion. El `select(.key == "mape_oof" or .key == "mape") | head -n1`
toma `mape_oof` si existe y `mape` como fallback para runs antiguos.

**Por que `MODEL_NAME` en vez de `VARIETY`**: en MLflow Registry los
modelos se guardan con el nombre completo `rnd-forest-<VARIETY>` (el
prefijo viene de `MODEL_REGISTRY_PREFIX` en `src/config.py`). Pedir
`MODEL_NAME=rnd-forest-POP` (y no `VARIETY=POP` con string concat
oculta) hace explicito que el parametro es el nombre real del registry,
y permite promover modelos que no sigan ese prefijo si manana cambia.

**Por que 3 gates (absoluto + A/B + transition)**: defense in depth.
El gate absoluto (`MAPE <= MAX_MAPE`) atrapa modelos catastroficamente
malos. El A/B (`MAPE_candidato < MAPE_production`) atrapa modelos que
pasan el absoluto pero degradan respecto al baseline real. Si no hay
Production previo, el A/B se saltea (primer deploy del modelo). El
3er paso recien hace `transition-stage` con `archive_existing=true`:
solo UNA version puede estar "Production" a la vez (las anteriores
quedan en `Archived`, accesibles pero no servidas).

**Por que `_mlflow-uri` prefiere `MLFLOW_ALB_DNS` antes de
`terraform output`**: GitHub Actions no corre `terraform init` en
cada job (ahorra ~30s + necesita credenciales para el backend S3).
La var `vars.MLFLOW_ALB_DNS` que el workflow exporta como env permite
que el promote corra sin tocar Terraform. Para uso local, el fallback
a `terraform output -raw alb_dns` sigue funcionando.

### 4.1.9 `tasks/aws.yml` (orquestadores high-level)

```yaml
# =============================================================================
# tasks/aws.yml  -  Orquestadores high-level del stack AWS
# =============================================================================
# Incluido por Taskfile.yml raiz con namespace "aws:".
# Son ATAJOS que encadenan tasks de otros namespaces (infra, ecr, batch,
# cluster) para los flujos completos del runbook.
#
# USO TIPICO:
#   task aws:deploy                                   stand-up (storage -> imagenes -> resto)
#   task aws:smoke                                    deploy + smoke test
#   task aws:wake                                     encender stack (lunes manana)
#   task aws:sleep                                    apagar stack (viernes noche)
#   task aws:teardown                                 destroy volatiles, preserva storage
#   task aws:rebuild                                  re-apply tras teardown
#   task aws:destroy                                  DESTRUCTIVO total
#   task aws:nuke                                     IRREVERSIBLE: destroy + tfstate + tflock + OIDC
#   task aws:status                                   outputs + cluster status
# =============================================================================

version: "3"

vars:
  SUFFIX:
    sh: aws sts get-caller-identity --query Account --output text | tail -c 7

tasks:

  # ═══ Deploy / smoke ═════════════════════════════════════════════════════════

  deploy:
    desc: "Stand-up completo: storage -> 3 imagenes -> resto (oleadas A+B+C)"
    cmds:
      - 'echo ">>> Oleada A: apply module.storage (S3 + ECR)..."'
      - task: ":infra:apply"
        vars: { TARGET: module.storage }
      - 'echo ">>> Oleada B: build + push 3 imagenes..."'
      - task: ":ecr:build-all"
      - 'echo ">>> Oleada C: apply resto (network, mlflow, batch, monitoring, ...)..."'
      - task: ":infra:apply"
      - 'echo ""'
      - 'echo "Deploy completo. ALB DNS:"'
      - task: ":infra:output-raw"
        vars: { NAME: alb_dns }
      - 'echo ""'

  smoke:
    desc: "Deploy + smoke test (POP, tuning=smoke, ~1 min)"
    cmds:
      - task: deploy
      - 'echo ">>> Smoke test..."'
      - task: ":batch:smoke"

  # ═══ Lifecycle (atajos a cluster:) ══════════════════════════════════════════

  wake:
    desc: "Encender stack (scale-up + wait-healthy). Para lunes a la manana"
    cmds:
      - task: ":cluster:scale-up"
      - task: ":cluster:wait-healthy"

  sleep:
    desc: "Apagar stack (scale-down). Para viernes a la noche / fuera de horario"
    cmds:
      - task: ":cluster:scale-down"

  teardown:
    desc: "scale-down + destroy modulos volatiles (preserva storage + network)"
    cmds:
      - task: ":cluster:teardown"

  rebuild:
    desc: "Re-apply de modulos volatiles + scale-up (reverso del teardown)"
    cmds:
      - task: ":cluster:rebuild"

  # ═══ Destroy total ══════════════════════════════════════════════════════════

  destroy:
    desc: "DESTRUCTIVO TOTAL: terraform destroy de modulos administrados. Doble confirmacion"
    prompt: "Destruira envs/prod (S3 + ECR + RDS + ...). Irreversible. Continuar?"
    cmds:
      - 'echo ">>> Drenando Batch jobs primero..."'
      - task: ":cluster:scale-down"
      - 'echo ""'
      - 'echo ">>> Vaciando buckets S3 con versioning (data + artifacts)..."'
      - task: _empty-bucket
        vars: { BUCKET: '{{.PROJECT}}-data-{{.SUFFIX}}' }
      - task: _empty-bucket
        vars: { BUCKET: '{{.PROJECT}}-artifacts-{{.SUFFIX}}' }
      - 'echo ""'
      - 'echo ">>> Borrando imagenes ECR..."'
      - task: _purge-ecr
        vars: { REPO: '{{.PROJECT}}' }
      - task: _purge-ecr
        vars: { REPO: '{{.PROJECT}}-mlflow' }
      - task: _purge-ecr
        vars: { REPO: '{{.PROJECT}}-reports' }
      - 'echo ""'
      - 'echo ">>> terraform destroy total..."'
      - task: ":infra:destroy"

  nuke:
    desc: "DESTRUCTIVO IRREVERSIBLE: destroy + borrar tfstate bucket + tflock + OIDC provider"
    prompt: "NUKE COMPLETO: destruira todo, incluido el state remoto (tfstate bucket + tflock) y el OIDC provider. Despues necesitas re-bootstrap. Continuar?"
    cmds:
      - task: destroy
      - 'echo ""'
      - 'echo ">>> Borrando bucket tfstate (backend Terraform)..."'
      - task: _empty-bucket
        vars: { BUCKET: '{{.PROJECT}}-tfstate-{{.SUFFIX}}', DELETE: "true" }
      - 'echo ">>> Borrando DynamoDB tflock..."'
      - task: _delete-tflock
      - 'echo ">>> Borrando OIDC provider de GitHub Actions..."'
      - task: _delete-oidc
      - 'echo ""'
      - 'echo "NUKE COMPLETO. Para volver: task infra:bootstrap + task infra:bootstrap-oidc + task aws:deploy"'

  # ═══ Helpers internos del destroy/nuke (no llamar directo) ══════════════════

  _empty-bucket:
    internal: true
    requires:
      vars: [BUCKET]
    vars:
      DELETE: '{{.DELETE | default "false"}}'
    cmds:
      - |
        if ! aws s3api head-bucket --bucket "{{.BUCKET}}" 2>/dev/null; then
          echo "  {{.BUCKET}} no existe, skip"; exit 0
        fi
        echo "  Vaciando {{.BUCKET}} (versiones + delete markers)..."
        aws s3api delete-objects --bucket "{{.BUCKET}}" \
          --delete "$(aws s3api list-object-versions --bucket "{{.BUCKET}}" \
            --query '{Objects: [Versions[].{Key:Key,VersionId:VersionId},DeleteMarkers[].{Key:Key,VersionId:VersionId}][]}' \
            --max-items 1000)" 2>/dev/null || echo "  (bucket ya vacio)"
        if [ "{{.DELETE}}" = "true" ]; then
          echo "  Borrando bucket {{.BUCKET}}..."
          aws s3 rb "s3://{{.BUCKET}}"
        fi

  _purge-ecr:
    internal: true
    requires:
      vars: [REPO]
    cmds:
      - |
        if ! aws ecr describe-repositories --repository-names "{{.REPO}}" >/dev/null 2>&1; then
          echo "  {{.REPO}} no existe, skip"; exit 0
        fi
        IDS=$(aws ecr list-images --repository-name "{{.REPO}}" --query 'imageIds[*]' --output json)
        if [ "$IDS" = "[]" ]; then
          echo "  {{.REPO}} vacio"; exit 0
        fi
        echo "  Borrando todas las imagenes de {{.REPO}}..."
        aws ecr batch-delete-image --repository-name "{{.REPO}}" --image-ids "$IDS" >/dev/null

  _delete-tflock:
    internal: true
    vars:
      TABLE: '{{.PROJECT}}-tflock'
    cmds:
      - |
        if aws dynamodb describe-table --table-name "{{.TABLE}}" >/dev/null 2>&1; then
          aws dynamodb delete-table --table-name "{{.TABLE}}" >/dev/null
          echo "  {{.TABLE}} borrada"
        else
          echo "  {{.TABLE}} no existe, skip"
        fi

  _delete-oidc:
    internal: true
    cmds:
      - |
        ARN=$(aws iam list-open-id-connect-providers \
          --query 'OpenIDConnectProviderList[?contains(Arn, `token.actions.githubusercontent.com`)].Arn' \
          --output text)
        if [ -z "$ARN" ]; then
          echo "  OIDC provider no existe, skip"; exit 0
        fi
        echo "  Borrando OIDC provider: $ARN"
        aws iam delete-open-id-connect-provider --open-id-connect-provider-arn "$ARN"

  # ═══ Estado ═════════════════════════════════════════════════════════════════

  status:
    desc: "Estado completo: outputs de Terraform + cluster:status"
    cmds:
      - 'echo "=== Terraform outputs ==="'
      - task: ":infra:output"
      - 'echo ""'
      - 'echo "=== Cluster ==="'
      - task: ":cluster:status"
```

**Por que existe esta capa `aws:`** (en lugar de invocar
`task infra:apply ...` + `task ecr:build-all` + `task infra:apply` a mano
cada vez):

- **Un comando, un flujo**: `task aws:deploy` reemplaza recordar 3 pasos
  con sus argumentos. Si tuvieras que recordarlos siempre, es facil
  olvidar el `TARGET=module.storage` del primer paso y romper la oleada.
- **Documentacion ejecutable**: leer `tasks/aws.yml` te dice exactamente
  como se hace un deploy. La guia describe el flujo, pero el codigo
  vive en un solo lugar.
- **Composition reusable**: `aws:smoke` reusa `aws:deploy` + `batch:smoke`.
  Si manana cambia el orden de oleadas (ej. agregamos una nueva), solo
  se toca `aws:deploy` y todo lo que depende se beneficia.

**Por que `aws:wake` encadena `cluster:scale-up` + `cluster:wait-healthy`
sin sleep intermedio**: RDS tarda ~5 min en estar Available desde
"stopped", pero `cluster:wait-healthy` ya hace polling del ALB cada 15s
con timeout de 10 min. Los primeros polls van a fallar mientras RDS
arranca; eso es esperado. Agregar un `sleep 300` extra solo bloquea la
terminal sin feedback. Si el deploy tarda mas que el timeout, el wait
falla con instruccion clara para revisar `aws logs tail`.

**Por que `aws:destroy` tiene doble confirmacion**: la primera viene de
su propio `prompt:`, la segunda de `infra:destroy` que invoca por
dentro. Es intencional: destruir storage versionado es la operacion mas
peligrosa del runbook, vale la pena un segundo pulse.

**Por que `aws:destroy` hace 4 cosas antes del `terraform destroy`** (en
vez de delegar todo a Terraform): los buckets con versioning enabled y
los repos ECR con imagenes adentro **NO se destruyen** con
`terraform destroy` plain — Terraform marca el recurso para borrado pero
AWS rechaza con `BucketNotEmpty`/`RepositoryNotEmpty`. El helper
`_empty-bucket` (borra todas las versiones + delete markers) y
`_purge-ecr` (borra todas las imagenes) hacen el cleanup previo. El
`cluster:scale-down` drena Batch jobs activos para evitar que el destroy
del job-queue falle con "queue has running jobs".

**Por que `_empty-bucket` toma el nombre completo del bucket + flag
`DELETE`** (en vez de dos helpers separados `_empty-bucket` y
`_delete-tfstate-bucket`): el truncamiento al sufijo de cuenta vive en
el caller (cada `task: _empty-bucket` arma `{{.PROJECT}}-data-{{.SUFFIX}}`
explicitamente). Un solo helper parametrizado con `DELETE=true` para el
caso nuke (vaciar + `aws s3 rb`) y `DELETE=false` para `aws:destroy`
(solo vaciar, Terraform se encarga del `rb`) elimina ~30 lineas de
duplicacion entre los dos paths sin perder claridad.

**Por que existe `aws:nuke` separado de `aws:destroy`** (y no un flag
`--nuke` del destroy): son 2 niveles de blast radius muy distintos.
`destroy` borra lo que el equipo de prod administra (modulos volatiles +
storage + ECR). `nuke` ademas borra el **backend de Terraform** (bucket
tfstate + DynamoDB tflock) y el **OIDC provider** que comparten todos
los repos GitHub de la cuenta — si tenes otros proyectos usando el mismo
OIDC, los rompes. `nuke` es para tear-down de cuenta entera o demo
descartable; despues requiere correr `infra:bootstrap` +
`infra:bootstrap-oidc` desde cero.

### 4.1.10 Anadir `includes:` al Taskfile raiz + verificacion final

Ahora que los 7 `tasks/*.yml` existen (§4.1.4 a §4.1.9 + §4.1.11),
agregar el bloque `includes:` al `Taskfile.yml` raiz, **despues de
`dotenv:` y antes de `vars:`**:

```yaml
# === ANADIR despues del bloque dotenv existente ===
# Modulos AWS por dominio. Cada include prefija con su namespace, asi las
# tasks locales (build, up, train, ...) no chocan con las AWS (infra:apply,
# ecr:build, ...).
#
# Variables PROJECT y REGION se propagan explicitamente: en go-task los
# includes tienen scope aislado, asi que sin `vars:` aqui los hijos no las
# verian. Esto centraliza los defaults y elimina redeclaraciones por archivo.
includes:
  infra:
    taskfile: ./tasks/infra.yml
    vars:
      PROJECT: '{{.PROJECT}}'
      REGION: '{{.REGION}}'
  ecr:
    taskfile: ./tasks/ecr.yml
    vars:
      PROJECT: '{{.PROJECT}}'
      REGION: '{{.REGION}}'
  batch:
    taskfile: ./tasks/batch.yml
    vars:
      PROJECT: '{{.PROJECT}}'
      REGION: '{{.REGION}}'
  cluster:
    taskfile: ./tasks/cluster.yml
    vars:
      PROJECT: '{{.PROJECT}}'
      REGION: '{{.REGION}}'
  mlflow-aws:
    taskfile: ./tasks/mlflow_registry.yml
    vars:
      PROJECT: '{{.PROJECT}}'
      REGION: '{{.REGION}}'
  aws:
    taskfile: ./tasks/aws.yml
    vars:
      PROJECT: '{{.PROJECT}}'
      REGION: '{{.REGION}}'
  local:
    taskfile: ./tasks/local.yml
    vars:
      PROJECT: '{{.PROJECT}}'
      REGION: '{{.REGION}}'
```

Verificacion:

```bash
# Lista plana de todas las tasks. Deberian aparecer namespaces
# infra:*, ecr:*, batch:*, cluster:*, mlflow-aws:*, aws:*, local:*
task --list

# Indice guiado del proyecto (local + AWS)
task

# Validar sintaxis de TODOS los Taskfiles sin ejecutar nada
task --list-all > /dev/null && echo "OK"
```

Si `task --list` muestra los 7 namespaces, el setup esta completo. A
partir de aca, las oleadas A/B/C (§4.2 a §4.5) usan estas tasks.

### 4.1.11 `tasks/local.yml` (helpers para desarrollo local que toca AWS)

Este Taskfile es opcional: existe para el flujo "dev local que quiere
sincronizar artifacts a S3 sin levantar todo Terraform". Si solo entrenas
en AWS Batch (que ya crea los buckets via `module.storage`), no lo
necesitas. Sirve cuando:

- Estas iterando un trainer local (`task train` o `python -m src.cli`)
  y queres que los artifacts (modelos `.joblib`, plots, reports) suban a
  S3 para verlos desde Reports nginx o para que un colega los levante.
- Todavia no aplicaste `module.storage` (la cuenta esta vacia) pero
  queres tener los buckets ya, idempotentemente.

Crear el archivo con este contenido:

```yaml
# =============================================================================
# tasks/local.yml  -  Helpers para desarrollo local que toca AWS
# =============================================================================
# Incluido por Taskfile.yml raiz con namespace "local:".
#
# USO TIPICO:
#   task local:ensure-buckets        crea data + artifacts S3 si no existen (idempotente)
#                                    Reusa los nombres de prod ({project}-data-<suffix>),
#                                    asi un sync local puede compartir bucket con AWS
#                                    Batch o no, segun como exportes S3_ARTIFACTS_BUCKET.
#   task local:bucket-name           imprime el nombre completo de un bucket
#                                    (var: KIND=data|artifacts)
# =============================================================================

version: "3"

vars:
  SUFFIX:
    sh: aws sts get-caller-identity --query Account --output text | tail -c 7

tasks:

  ensure-buckets:
    desc: "Crea S3 buckets data + artifacts si no existen (idempotente). Misma cuenta+region que prod."
    silent: true
    cmds:
      - task: _ensure-bucket
        vars: { NAME: '{{.PROJECT}}-data-{{.SUFFIX}}' }
      - task: _ensure-bucket
        vars: { NAME: '{{.PROJECT}}-artifacts-{{.SUFFIX}}' }
      - 'echo ""'
      - 'echo "Listo. Para que el trainer local sincronice a estos buckets, exporta:"'
      - 'echo "  export S3_DATA_BUCKET={{.PROJECT}}-data-{{.SUFFIX}}"'
      - 'echo "  export S3_ARTIFACTS_BUCKET={{.PROJECT}}-artifacts-{{.SUFFIX}}"'

  bucket-name:
    desc: "Imprime el nombre del bucket. Var: KIND=data|artifacts (REQ)"
    silent: true
    requires:
      vars: [KIND]
    cmds:
      - echo "{{.PROJECT}}-{{.KIND}}-{{.SUFFIX}}"

  _ensure-bucket:
    internal: true
    silent: true
    requires:
      vars: [NAME]
    cmds:
      - |
        if aws s3api head-bucket --bucket "{{.NAME}}" 2>/dev/null; then
          echo "  {{.NAME}}  EXISTE (reuso)"
          exit 0
        fi
        echo "  {{.NAME}}  no existe -> creando..."
        # us-east-1 NO acepta --create-bucket-configuration (es default y AWS lo rechaza)
        if [ "{{.REGION}}" = "us-east-1" ]; then
          aws s3api create-bucket --bucket "{{.NAME}}" --region {{.REGION}}
        else
          aws s3api create-bucket --bucket "{{.NAME}}" --region {{.REGION}} \
            --create-bucket-configuration LocationConstraint={{.REGION}}
        fi
        # Hardening minimo (mismas defaults que el modulo storage de prod)
        aws s3api put-bucket-versioning --bucket "{{.NAME}}" \
          --versioning-configuration Status=Enabled
        aws s3api put-bucket-encryption --bucket "{{.NAME}}" \
          --server-side-encryption-configuration \
          '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
        aws s3api put-public-access-block --bucket "{{.NAME}}" \
          --public-access-block-configuration \
          'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'
        echo "  {{.NAME}}  CREADO (versioning + AES256 + no public)"
```

**Por que reusa el naming `{project}-{kind}-{suffix6}` de prod**: si el
modulo `storage` de Terraform ya creo los buckets, el `head-bucket` del
helper los detecta y hace `EXISTE (reuso)` — sin colisiones ni duplicacion.
Si la cuenta esta vacia, `ensure-buckets` los crea con las mismas defaults
(versioning + AES256 + sin acceso publico) que el modulo. La consecuencia:
el bucket que ves localmente es exactamente el que va a usar `module.storage`
cuando aplique, sin migracion de datos.

**Por que `local:` y no `aws:setup-local`**: filosofica. El namespace `aws:`
es para orquestar el stack en la nube (deploy/wake/sleep/destroy); `local:`
es para tu maquina. Si manana se agregan helpers tipo
`local:download-latest-model` o `local:sync-data-from-s3`, viven naturalmente
aca y no pisan el namespace de orquestacion.

## 4.2 Ola A — apply storage solo

Crea los 2 buckets S3 + 3 repos ECR. Tiempo: ~1 min.

```bash
# Variables de sesion (re-declaradas para que cada oleada sea standalone
# copy-paste-able; si ya las exportaste en Capítulo 3.5 estas lineas son no-op).
export AWS_DEFAULT_REGION="us-east-1"
export PROJECT="ml-training"
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export ACCOUNT_SUFFIX="${ACCOUNT_ID: -6}"

# Apply solo el modulo storage
task infra:apply TARGET=module.storage
```

### Verificacion Ola A

```bash
# Esperado: los 3 repos ECR existen
aws ecr describe-repositories \
    --repository-names ml-training ml-training-mlflow ml-training-reports \
    --query 'repositories[].repositoryUri' --output table

# Esperado: los 2 buckets existen y tienen encryption + versioning
export DATA_BUCKET="${PROJECT}-data-${ACCOUNT_SUFFIX}"
export ARTIFACTS_BUCKET="${PROJECT}-artifacts-${ACCOUNT_SUFFIX}"

aws s3api get-bucket-versioning --bucket "$DATA_BUCKET"      --query Status --output text
aws s3api get-bucket-versioning --bucket "$ARTIFACTS_BUCKET" --query Status --output text
# Esperado para ambos: Enabled
```

### Subir el Excel inicial al bucket de data

Antes del primer training real, el bucket `data` necesita el Excel:

```bash
# Asume que tenes data/BD_HISTORICO_ACUMULADO.xlsx en local (workflow normal)
aws s3 cp data/BD_HISTORICO_ACUMULADO.xlsx \
    "s3://${DATA_BUCKET}/BD_HISTORICO_ACUMULADO.xlsx"

# Verificar
aws s3 ls "s3://${DATA_BUCKET}/" --human-readable
```

> **En consola AWS veras** despues de Ola A:
> - S3 → Buckets → `ml-training-data-<suffix>` (con
>   `BD_HISTORICO_ACUMULADO.xlsx` adentro) y `ml-training-artifacts-<suffix>`
>   (vacio).
> - ECR → Repositories → 3 (`ml-training`, `ml-training-mlflow`,
>   `ml-training-reports`) los 3 vacios — el push viene en Ola B.

## 4.3 Ola B — build + push 3 imagenes a ECR

Las 3 imagenes son:

| Imagen | Dockerfile | Tag | Para que |
|---|---|---|---|
| `ml-training` | `./Dockerfile` (raiz, ya existe) | `latest` + `sha-<git-sha>` | Trainer en AWS Batch |
| `ml-training-mlflow` | `./docker/mlflow/Dockerfile` (ya existe en local) | `v3.12.0` | MLflow server en Fargate |
| `ml-training-reports` | `./docker/reports/Dockerfile` (creado en 3.6.4) | `stable` | Nginx que sirve S3 |

### 4.3.1 Como funciona

La task `ecr:build-all` (definida en §4.1.5) encadena 3 invocaciones de
`ecr:build` con `IMG=trainer/mlflow/reports`. Cada una hace
`docker build` con build args (`GIT_SHA`, `BUILD_DATE`, `VERSION`) y
pushea 2 tags: el solicitado (`latest`/`v3.12.0`/`stable`) y
`sha-<git-sha-corto>` para rollback determinista.

### 4.3.2 Overrides via variables CLI

```bash
# Override del tag (e.g. bump version de MLflow)
task ecr:build IMG=mlflow TAG=v3.13.0

# Solo trainer (re-build despues de cambio de codigo)
task ecr:build IMG=trainer
```

### 4.3.3 Ejecutar

```bash
# Variables (recordatorio)
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# Build + push de las 3 imagenes (trainer + mlflow + reports)
task ecr:build-all
```

### Verificacion Ola B

```bash
# Las 3 imagenes con tag esperado existen en ECR
aws ecr list-images --repository-name ml-training --query 'imageIds[?imageTag==`latest`]'
aws ecr list-images --repository-name ml-training-mlflow --query 'imageIds[?imageTag==`v3.12.0`]'
aws ecr list-images --repository-name ml-training-reports --query 'imageIds[?imageTag==`stable`]'
```

Cada uno debe devolver un array con 1 item (no vacio).

> **En consola AWS veras** despues de Ola B:
> - ECR → Repositories → `ml-training` → Images: 2 tags
>   (`latest` + `sha-<12chars>`) con `imageSizeInBytes` >0 y
>   `imagePushedAt` reciente.
> - ECR → `ml-training-mlflow` → Images: 2 tags (`v3.12.0` + `sha-...`).
> - ECR → `ml-training-reports` → Images: 2 tags (`stable` + `sha-...`).
> - Cada imagen muestra el resultado del scan-on-push (vulnerabilities
>   findings: usualmente "No findings" en imagenes oficiales, algunos
>   MEDIUM/LOW en `ml-training` por las deps de Python).

## 4.4 Ola C — apply full (en 4 sub-olas con checkpoint)

Ahora todo el resto (`network`, `mlflow`, `reports`, `batch`,
`monitoring`, `lambdas`, `scheduler`, `cicd`). Tiempo total: ~15-20
min. El que mas demora: RDS create (~8 min) + ALB warmup + Fargate
task launch (~3 min).

**Por que se parte en 4 sub-olas**: un `terraform apply` monolitico
falla "en silencio" — si el modulo 5 de 8 explota, te enteras 18 min
despues. Partiendo por capa de dependencia, cada checkpoint da
feedback en 2-5 min y el error es localizable. Tambien permite saltar
a la siguiente sub-ola sin re-planear las anteriores.

### 4.4.1 Sub-ola C1 — `network` (red base, ~1-2 min)

```bash
task infra:plan TARGET=module.network
task infra:apply TARGET=module.network

# Checkpoint: VPC + 2 subnets + NAT + 4 SGs visibles
aws ec2 describe-vpcs \
    --filters "Name=tag:Project,Values=ml-training" \
    --query 'Vpcs[].VpcId' --output text
# Esperado: 1 VPC ID (vpc-XXXX)

aws ec2 describe-nat-gateways \
    --filter "Name=tag:Project,Values=ml-training" "Name=state,Values=available" \
    --query 'NatGateways[].NatGatewayId' --output text
# Esperado: 1 NAT GW ID
```

> **En consola AWS**: VPC console → Your VPCs → `ml-training-vpc`;
> Subnets → 2 (public + private); NAT Gateways → 1 (state=available);
> Security Groups → 4 (`ml-training-sg-alb`, `-sg-mlflow`, `-sg-batch`,
> `-sg-rds`).

### 4.4.2 Sub-ola C2 — `mlflow` + `reports` (RDS + ALB + 2 Fargate, ~10 min)

```bash
task infra:plan TARGET=module.mlflow
task infra:apply TARGET=module.mlflow      # ~8 min (RDS create domina)

task infra:plan TARGET=module.reports
task infra:apply TARGET=module.reports     # ~2 min

# Checkpoint: ALB responde + RDS available
export ALB="$(terraform -chdir=infra/envs/prod output -raw alb_dns)"
curl -sI "http://${ALB}/" | head -1         # esperado: HTTP/1.1 200 OK
curl -sI "http://${ALB}/reports/" | head -1 # esperado: HTTP/1.1 200 OK
```

> **En consola AWS**: RDS → Databases → `ml-training-mlflow`
> (status=Available); ECS → Clusters → `ml-training-cluster` → Services
> (mlflow + reports, runningCount=1 cada uno); EC2 → Load Balancers →
> `ml-training-alb`.

### 4.4.3 Sub-ola C3 — `batch` + `lambdas` (compute + orquestacion, ~3 min)

```bash
task infra:apply TARGET=module.batch
task infra:apply TARGET=module.lambdas

# Checkpoint: queues VALID + 2 lambdas listadas
aws batch describe-job-queues \
    --query "jobQueues[?starts_with(jobQueueName,'ml-training')].[jobQueueName,status]" \
    --output table
# Esperado: 2 queues con status=VALID

aws lambda list-functions \
    --query "Functions[?starts_with(FunctionName,'ml-training-')].FunctionName" \
    --output table
# Esperado: ml-training-dispatcher, ml-training-notifier (scheduler aparece en C4)
```

> **En consola AWS**: Batch → Job queues (2: spot + ondemand, ambos
> VALID); Compute environments (2: ml-training-ce-spot, -ondemand);
> Job definitions → `ml-training-trainer`; Lambda → Functions (2:
> dispatcher + notifier).

### 4.4.4 Sub-ola C4 — `monitoring` + `scheduler` + `cicd` (~2 min)

```bash
task infra:apply TARGET=module.monitoring
task infra:apply TARGET=module.scheduler
task infra:apply TARGET=module.cicd

# Checkpoint: alarmas + crons + roles GHA
aws cloudwatch describe-alarms \
    --alarm-name-prefix ml-training \
    --query 'MetricAlarms[].AlarmName' --output table
# Esperado: N + 2 alarmas, donde N = length(var.varieties) (lo que pusiste
# en terraform.tfvars; cualquier numero >= 1 es valido):
#   - ml-training-batch-failed                              (siempre 1)
#   - ml-training-mape-<variety>  por cada variedad         (N copies)
#   - ml-training-alb-5xx                                   (siempre 1)
# Validacion programatica del conteo (independiente del valor de N):
COUNT=$(aws cloudwatch describe-alarms --alarm-name-prefix ml-training \
        --query 'length(MetricAlarms)' --output text)
N=$(aws cloudwatch describe-alarms --alarm-name-prefix ml-training-mape- \
        --query 'length(MetricAlarms)' --output text)
test "$COUNT" -eq "$((N + 2))" && echo "OK: $COUNT alarmas = $N variedades + 2" \
                              || echo "MISMATCH: $COUNT total, $N MAPE -> esperaba N+2=$((N+2))"

aws events list-rules \
    --query "Rules[?starts_with(Name,'ml-training-')].Name" --output table
# Esperado: ml-training-start, -stop, -rds-keepstop, -batch-failed

aws iam list-roles \
    --query "Roles[?starts_with(RoleName,'ml-training-gha-')].RoleName" --output table
# Esperado: ml-training-gha-deploy, ml-training-gha-train
```

> **En consola AWS**: SNS → Topics → `ml-training-alerts`; CloudWatch
> → Alarms (**N + 2** donde N = `length(var.varieties)`: batch-failed +
> mape-<variety> × N + alb-5xx; el conteo escala automatico si agregas
> o quitas variedades); EventBridge → Rules (4: start, stop, rds-keepstop,
> batch-failed); Lambda → `ml-training-scheduler`; IAM → Roles
> (`gha-deploy`, `gha-train`).

### 4.4.5 Apply full alternativo (cuando ya pasaste por C1-C4 una vez)

Para re-applies idempotentes (despues de algun cambio menor), una vez
validado que todo arriba existe, podes usar:

```bash
task infra:plan
task infra:apply
```

> **Cuando usar el apply monolitico**: re-deploys post-stand-up. NUNCA
> en el primer stand-up — si algun modulo falla, debug es mucho mas
> caro.

### Recovery comun durante Ola C

| Sintoma | Causa probable | Fix |
|---|---|---|
| `RDSCreate` cuelga 15+ min | Subnet group sin AZs distintas o no hay capacity en la AZ | Re-apply (idempotente); si persiste, reduce a 1 AZ en module.network |
| `aws_ecs_service.mlflow: timeout waiting for steady state` | La imagen MLflow no esta en ECR o el comando rompe en startup | Re-corre 4.3.3; revisa `aws logs tail /ecs/ml-training/mlflow --follow` |
| `aws_lambda_function: source_code_hash mismatch` | Editaste el .py pero no re-zip-eo | `terraform apply` lo detecta y re-zipea (idempotente) |
| `permission denied: iam:CreateRole` | Tu profile AWS no tiene IAM permissions | `aws sts get-caller-identity` y revisa que sea admin/role-with-IAM |
| State lock acquire timeout | Otro `terraform apply` corriendo / state lock huerfano | `terraform force-unlock <LOCK_ID>` (mostrado en el error) |

### Verificacion Ola C

```bash
# 1) Outputs del envs/prod
cd infra/envs/prod
terraform output
cd ../../..

# 2) ALB DNS responde (puede que MLflow aun este iniciando)
export ALB="$(terraform -chdir=infra/envs/prod output -raw alb_dns)"
curl "http://${ALB}/"         # esperado: HTTP/1.1 200 OK con HTML de MLflow
curl "http://${ALB}/reports/" # esperado: 200 (autoindex de nginx)

# 3) Lambdas listadas
aws lambda list-functions \
    --query "Functions[?starts_with(FunctionName,'ml-training-')].FunctionName" --output table

# 4) EventBridge rules listadas
aws events list-rules \
    --query "Rules[?starts_with(Name,'ml-training-')].Name" --output table

# 5) RDS available
aws rds describe-db-instances \
    --db-instance-identifier ml-training-mlflow \
    --query 'DBInstances[0].DBInstanceStatus' --output text
# Esperado: available
```

Si los 5 checks dan OK, la infra esta arriba.

## 4.5 Smoke test — entrenar 1 variedad end-to-end

Esto verifica (el item 1 sobre Lambda dispatcher se valida indirectamente
en §4.7.1 cuando uses `task batch:train`; el smoke va directo a Batch):

1. Batch submit funciona (SubmitJob directo, sin pasar por Lambda).
2. EC2 Spot arranca + corre el container.
3. El trainer hydrate-a la data desde S3.
4. Logs llegan a CloudWatch.
5. MLflow registra el run.
6. Outputs syncan a S3.
7. Dashboards visibles en `/reports/`.
8. Custom metric MAPE publicada (despues de Parte 5; en este smoke
   no se valida todavia).

### 4.5.1 Como funciona la task `batch:smoke`

`batch:smoke` es un atajo a `batch:train VARIETIES=POP TUNING=smoke`. La
logica vive en `tasks/batch.yml` (un solo loop bash que hace submit +
polling por cada variedad):

1. **Submit** via `aws batch submit-job` con la job-definition
   `ml-training-trainer` y container overrides `--varieties POP
   --tuning smoke --parallel-varieties 1`.
2. **Polling** via `aws batch describe-jobs` cada 30s hasta que `status`
   sea `SUCCEEDED` (exit 0) o `FAILED` (exit 1).

**Por que NO via Lambda dispatcher** (a diferencia del train via
GitHub Actions): la task local hace submit directo a Batch para tener
control fino del exit code y diagnostico en la terminal. El Lambda
dispatcher es para invocaciones desde cron/EventBridge donde no hay un
humano esperando.

### 4.5.2 Ejecutar

```bash
task batch:smoke
```

Tiempo total esperado: **10-15 min** desde invoke hasta `SUCCEEDED`.
Breakdown:
- Lambda invoke + submit: <5 s
- EC2 Spot provisioning: 2-5 min
- Container pull (primera vez ~3 GB): 3-5 min (cached despues)
- Trainer ejecucion `--tuning smoke`: 2-4 min (smoke = 5 iter Optuna)
- S3 sync + container shutdown: ~30 s

### 4.5.3 Verificacion post-smoke

```bash
export ALB="$(terraform -chdir=infra/envs/prod output -raw alb_dns)"
export ARTIFACTS_BUCKET="${PROJECT}-artifacts-${ACCOUNT_SUFFIX}"

# 1) MLflow tiene el run
curl "http://${ALB}/api/2.0/mlflow/experiments/search" \
    -X POST -H "Content-Type: application/json" \
    -d '{}'
# Esperado: al menos un experimento llamado "POP" con runs.id

# 2) S3 tiene los artifacts
aws s3 ls "s3://${ARTIFACTS_BUCKET}/artifacts/" --recursive --human-readable | grep POP
# Esperado: final_pipeline_POP_*.joblib + run_summary_POP*.json

# 3) S3 tiene los reports
aws s3 ls "s3://${ARTIFACTS_BUCKET}/reports/" --recursive | grep POP
# Esperado: dashboard_POP.html

# 4) /reports/POP/ accesible via ALB
curl "http://${ALB}/reports/POP/"   # esperado: HTML del dashboard

# 5) Custom metric MAPE publicada (despues de Parte 5, no ahora)
# Para esta primera vuelta sin patch del trainer, NO esperar metricas
# en namespace "ml-training/Training" todavia.
```

Si (1) y (2) salen OK, **el smoke pasa**. (3) y (4) tambien deberian
salir OK porque `main.py:scripts.s3_sync.sync_to_s3` ya sube reports
si `S3_ARTIFACTS_BUCKET` esta seteado (ya esta, via job-def).

## 4.6 Confirmar suscripcion SNS

SNS manda un email de confirmacion cuando creas la suscripcion (Parte
3.8). Tenes que clickear el link para activarla.

```bash
# Resolver el TopicArn una vez (bash command substitution con $(...))
TOPIC_ARN="$(aws sns list-topics \
    --query "Topics[?contains(TopicArn,'ml-training-alerts')].TopicArn" \
    --output text)"

# Estado de la suscripcion
aws sns list-subscriptions-by-topic \
    --topic-arn "${TOPIC_ARN}" \
    --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output table
```

Si `SubscriptionArn` dice `PendingConfirmation`, revisa el mail
(`abantodca@gmail.com`) y clickea "Confirm subscription".

Test:

```bash
aws sns publish \
    --topic-arn "${TOPIC_ARN}" \
    --subject "TEST: ml-training alerts" \
    --message "Si recibis este mail, la suscripcion esta OK."
```

## 4.7 Tasks operativas (referencia)

Esto materializa los comandos que la Parte 1 (Lifecycle) menciono como
contrato. La implementacion completa de cada task ya se copio inline en
§4.1.4 a §4.1.9 + §4.1.11 (`tasks/infra.yml`, `tasks/ecr.yml`,
`tasks/batch.yml`, `tasks/cluster.yml`, `tasks/mlflow_registry.yml`,
`tasks/aws.yml`, `tasks/local.yml`). Esta seccion es el **catalogo de
uso** de esas tasks ya creadas.

### 4.7.1 Re-entrenamiento (`task batch:train`)

Submit + polling automatico hasta `SUCCEEDED`/`FAILED`. Acepta una o N
variedades (serie, con espera entre cada una). Si pasas `WAIT=false`,
dispara y vuelve (el notifier ya manda mail al terminar).

```bash
# Re-entrenar POP en prod (espera hasta terminar)
task batch:train VARIETIES=POP

# Multi variedad (lanza N jobs en serie con espera entre cada uno)
task batch:train VARIETIES=POP,JUPITER

# Fire-and-forget (no esperes — el notifier ya manda mail)
task batch:train VARIETIES=POP WAIT=false

# prod_xl -> queue On-Demand (~5-6h, evita kills Spot)
task batch:train VARIETIES=POP TUNING=prod_xl
```

**Por que la queue cambia con TUNING**: `prod_xl` corre 5-6h y la
probabilidad de kill Spot a esa duracion es 20-30%. La logica vive en
`tasks/batch.yml` (template `{{if eq .TUNING "prod_xl"}}-ondemand{{else}}-spot{{end}}`).

### 4.7.2 Apagar servicios (`task cluster:scale-down` / `task aws:sleep`)

Lo que el cron L-V 12:00 PET hace automaticamente, pero invocado a mano.
Chequea Batch jobs RUNNING antes de tocar nada — si hay, aborta con
exit 1 (no usa `--force`; preferimos fallar visible).

```bash
# Atajo de alto nivel
task aws:sleep

# Equivalente granular
task cluster:scale-down
```

**Por que invoca el Lambda dispatcher y no `aws lambda invoke` crudo**:
la logica (drenar Batch -> apagar Fargate -> stop RDS en orden) ya vive
en `infra/lambdas/dispatcher.py`. Re-implementarla en bash duplicaria
mantenimiento.

### 4.7.3 Encender servicios (`task cluster:scale-up` / `task aws:wake`)

```bash
# Atajo high-level: scale-up + sleep 300 + wait-healthy
task aws:wake

# Granular (sin esperar al ALB)
task cluster:scale-up

# Solo esperar ALB (asumiendo scale-up corrio antes)
task cluster:wait-healthy
```

**Por que `sleep 300` en `aws:wake`**: RDS tarda ~5 min en estar Available
desde "stopped". `wait-healthy` chequea el ALB cada 15s hasta 10 min;
sin el sleep previo, los primeros 20 polls fallarian inutilmente.

### 4.7.4 Teardown (`task cluster:teardown` / `task aws:teardown`)

Encadena `scale-down` + `terraform destroy -target=module.X` para los
modulos volatiles. Preserva storage + ECR + network + state.

```bash
task aws:teardown
# Pide confirmacion: "Esto destruira los modulos volatiles..."
```

Modulos destruidos (en orden reverso de apply para respetar dependencias):

```
module.scheduler -> module.lambdas -> module.monitoring
-> module.batch -> module.reports -> module.mlflow
```

**Por que NO destruye `module.network`**: el NAT Gateway dentro de
network cuesta $32/mes encendido pero su destroy/create tarda 5+ min
cada vuelta. Si vas a teardown frecuentemente (>1 vez/mes), preserva
el NAT. Si vas a hibernar largo (>3 meses), incluilo manualmente:
`task infra:destroy-target TARGET=module.network`.

### 4.7.5 Rebuild (`task cluster:rebuild` / `task aws:rebuild`)

Reverso del teardown: re-apply de todos los modulos + scale-up.

```bash
task aws:rebuild
# Luego: task cluster:wait-healthy para confirmar
```

**Por que dispara `task infra:apply` (full) y no solo los modulos volatiles**:
Terraform es idempotente — los modulos no destruidos quedan no-op. Mas
simple que mantener una lista paralela de "modulos a re-apply".

### 4.7.6 Destroy total (`task aws:destroy` / `task infra:destroy`)

DESTRUCTIVO completo. Doble confirmacion (`prompt:` en `aws:destroy`
+ `prompt:` en `infra:destroy`).

```bash
task aws:destroy
# Confirma 2 veces. El primero pregunta antes de scale-down,
# el segundo antes del terraform destroy.
```

**No incluye** vaciado de buckets versionados, OIDC provider, ni DDB
tflock (esos son bootstrap manual, no Terraform). Para limpieza
completa hasta "cuenta como antes de Parte 2", seguir §8.7 a mano
despues de `task aws:destroy`.

### 4.7.7 Promote a Production (`task mlflow-aws:promote`)

Quality gate sobre MAPE antes de transicionar Staging -> Production:

```bash
# Listar versiones de un modelo
task mlflow-aws:list-versions VARIETY=POP

# Promover v3 con gate default (MAPE <= 20)
task mlflow-aws:promote VARIETY=POP VERSION=3

# Override del umbral
task mlflow-aws:promote VARIETY=POP VERSION=3 MAX_MAPE=15

# Ver la version Production actual
task mlflow-aws:current-prod VARIETY=POP
```

La task hace:

1. Resuelve `ALB_DNS` via `terraform output -raw alb_dns` (single source of truth).
2. `GET /api/2.0/mlflow/model-versions/get` -> obtiene `run_id`.
3. `GET /api/2.0/mlflow/runs/get` -> lee metric `mape_oof`.
4. Compara contra `MAX_MAPE` (default 20). Si supera, aborta con exit 1.
5. `POST /api/2.0/mlflow/model-versions/transition-stage` con `archive_existing_versions=true`.

**Por que via REST API y no `mlflow` CLI**: el host (Windows + WSL Ubuntu,
o Linux/Mac) no necesariamente tiene `mlflow` CLI. `curl + jq` son
ubicuos y la API REST es estable cross-version. Implementacion en
`tasks/mlflow_registry.yml`.

### 4.7.8 Resumen del catalogo

Mapping de los playbooks Ansible V1 (deprecated) a las tasks V2:

| Playbook V1 (deprecated) | Task V2 | Notas |
|---|---|---|
| `deploy.yml` | `task infra:apply [TARGET=...]` | Generic Terraform wrapper |
| `destroy.yml` | `task infra:destroy` o `task aws:destroy` | El de aws orquesta scale-down primero |
| `build_and_push.yml` | `task ecr:build-all` | O `task ecr:build IMG=trainer` |
| `smoke.yml` | `task batch:smoke` | POP + tuning=smoke (atajo a `batch:train`) |
| `retrain.yml` | `task batch:train VARIETIES=...` | 1 o N variedades, serial |
| `scale_down.yml` | `task cluster:scale-down` o `task aws:sleep` | |
| `scale_up.yml` | `task cluster:scale-up` o `task aws:wake` | aws:wake incluye wait-healthy |
| `teardown.yml` | `task cluster:teardown` o `task aws:teardown` | |
| `rebuild.yml` | `task cluster:rebuild` o `task aws:rebuild` | |
| `promote.yml` | `task mlflow-aws:promote VARIETY=X VERSION=N` | Gate MAPE built-in |
| `bootstrap_cicd.yml` | (no necesario en V2) | El modulo `cicd` ya esta en `envs/prod/main.tf` |

Ver `task --list` para el catalogo completo incluyendo helpers
(`infra:output`, `infra:validate`, `batch:status`, `ecr:list`, ...).

---

> **Cierre Parte 4.**
>
> Estado actual:
> - Infra desplegada en AWS, ALB respondiendo 200.
> - Smoke test OK (1 job de Batch entreno POP, modelos en MLflow Registry y S3).
> - 7 archivos `tasks/*.yml` con ~30 tasks AWS expuestas en `task --list`:
>   `infra:*` (apply/destroy/plan/bootstrap/validate), `ecr:*` (build/build-all/login/list),
>   `batch:*` (train/train-lambda/smoke/status/cancel), `cluster:*` (scale-up/down/teardown/rebuild/wait-healthy),
>   `mlflow-aws:*` (promote/list-versions/current-prod), `aws:*` (deploy/smoke/wake/sleep/teardown/rebuild/destroy/nuke/status),
>   `local:*` (ensure-buckets/bucket-name).
> - SNS confirmado (suscripcion email activa).
>
> Lo que falta para "produccion completa":
> - **Parte 5**: patch del trainer para emitir custom metric MAPE a
>   CloudWatch (alarmas dimensionadas en Parte 3.8 todavia no reciben datos).
> - **Parte 6**: workflows GitHub Actions (ci.yml, train.yml, promote.yml,
>   terraform-plan.yml).
> - **Parte 7**: gate de promotion automatico (A/B contra Production).
> - **Parte 8-12**: runbook extendido, costos, hardening, troubleshooting.
>
# Parte 5 — Patch del trainer (emitir MAPE a CloudWatch)

> **STATUS: APLICADO** (auditoria 2026-05-18). El patch esta integrado en `src/orchestration/variety_runner.py` (import + llamada `emit_mape_metric(variety=variety, mape_value=champion.oof_mape)` despues del bloque mape_ok/gap_ok). `trainer_image_tag` bumpeado a `v0.2.0` en `infra/envs/prod/terraform.tfvars`. La proxima vez que rebuiledes con `task ecr:build IMG=trainer` la imagen llevara el patch.

> **Por que este patch es necesario**: el modulo `monitoring` (3.8)
> creo alarmas que escuchan el namespace `ml-training/Training` con
> dimension `variety`. Sin este patch, las alarmas nunca disparan
> porque el namespace esta vacio — y un MAPE alto en POP pasa silencioso.
>
> **Diferencia con V1**: el V1 hardcoded la alarma a POP (`mape_pop`) y
> el patch emitia un solo metric sin dimension. El V2 emite UNA serie
> por variedad con dimension `variety=<NOMBRE>`, lo cual matchea las
> alarmas dinamicas de 3.8.

## 5.1 Donde se inserta el patch

Tu codigo actual: `src/orchestration/runners.py` (`run_parallel` /
`run_sequential`) llama al pipeline por variedad y al final cada uno
loguea metrics a MLflow. Aprovechamos ese mismo punto.

El patch crea una funcion nueva `_emit_mape_metric` y la invoca al
final del per-variety pipeline, despues del `mlflow.log_metric`.

## 5.2 Crear `src/utils/cloudwatch_metrics.py`

Archivo nuevo, no edita codigo existente:

```python
"""Emite custom metrics a CloudWatch.

Solo se activa si AWS_DEFAULT_REGION + S3_ARTIFACTS_BUCKET estan
seteados (lo cual indica que estamos en AWS Batch, no en local).
En local es no-op.
"""
from __future__ import annotations

import logging
import os
from typing import Final

log = logging.getLogger(__name__)

NAMESPACE: Final[str] = "ml-training/Training"


def emit_mape_metric(variety: str, mape_value: float) -> None:
    """Publica MAPE a CloudWatch con dimension `variety`.

    No falla el training si la publicacion falla (best-effort).
    """
    if not os.environ.get("S3_ARTIFACTS_BUCKET"):
        # Local: skip silencioso
        return

    try:
        import boto3
    except ImportError:
        log.warning("boto3 no instalado, skip CloudWatch metric")
        return

    try:
        cw = boto3.client("cloudwatch")
        cw.put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[{
                "MetricName": "MAPE",
                "Dimensions": [{"Name": "variety", "Value": variety}],
                "Value":      float(mape_value),
                "Unit":       "Percent",
            }],
        )
        log.info("CloudWatch MAPE=%.4f emitido (variety=%s)", mape_value, variety)
    except Exception as exc:
        log.warning("CloudWatch put_metric_data fallo: %s", exc)
```

## 5.3 Invocar desde el runner

Editar `src/orchestration/variety_runner.py` (no `runners.py` — ese
solo orquesta secuencial/paralelo; la logica por variedad vive aca).
La funcion a parchar es `train_variety`, justo despues del bloque del
quality gate (donde se loguea "CAMPEON pasa quality gate" o
"RECHAZADO por calidad operativa") y ANTES del bloque que genera el
Excel/Dashboard ejecutivo.

**Diff conceptual** (antes / despues):

```diff
  # ... bloque del quality gate termina con:
  #   args_register = ...   o   args_register = False

+ # NUEVO: emit a CloudWatch para alarma "MAPE alto" (modulo monitoring)
+ # Skip silencioso en local (S3_ARTIFACTS_BUCKET vacio).
+ emit_mape_metric(variety=variety, mape_value=champion.oof_mape)
+
  # Eliminar runs de modelos NO campeon de MLflow Experiments. ...
  losers = [r for r in results if r is not champion]
```

**Patch a aplicar** (en `src/orchestration/variety_runner.py`):

```python
# 1) Import al inicio del modulo, junto al resto de imports de src.utils
from src.utils.cloudwatch_metrics import emit_mape_metric

# 2) Dentro de train_variety, despues del bloque `if not mape_ok / elif
#    not gap_ok / else` (~linea 147 actual), antes de `losers = [...]`:
emit_mape_metric(variety=variety, mape_value=champion.oof_mape)
```

> **Por que aca y no en `runners.py`**: el champion existe como objeto
> `ModelResult` (dataclass del paso 05) solo dentro de `train_variety`.
> En `runners.py` solo se ve el `dict` agregado y serializado. Emitir
> aca tambien garantiza que se emite por variedad (la metric tiene
> `Dimensions=[{Name: variety, Value: variety}]` y la alarma de
> `modules/monitoring` justamente filtra por dimension).

> **Por que `champion.oof_mape` y no `champion.full_mape`**: la alarma
> mide degradacion en datos no vistos (OOF), que es lo que el negocio
> realmente experimenta. `full_mape` es in-sample (optimista) y mete
> ruido cuando el modelo memoriza el train.

## 5.4 Verificar local que no rompe

```bash
# Smoke local (S3_ARTIFACTS_BUCKET vacio -> emit hace skip silencioso)
docker compose run --rm trainer --varieties POP --tuning smoke
# Esperado: el log tiene "CloudWatch MAPE=..." cuando es prod, y nada
# en local. El training termina exitoso.
```

## 5.5 Commit + re-build + push

> **Nota**: esto YA fue aplicado en commit X. Si necesitas RE-publicar la imagen tras cambiar el codigo, segui los pasos abajo.

```bash
git add src/utils/cloudwatch_metrics.py src/orchestration/variety_runner.py
git commit -m "feat(monitoring): emit MAPE custom metric a CloudWatch con dim=variety"

# Re-build + push del trainer con el patch (bump version para que ECR retenga la anterior)
task ecr:build IMG=trainer TAG=v0.2.0
```

Y propagar la nueva tag a Batch:

```bash
# Editar terraform.tfvars: trainer_image_tag = "v0.2.0"
# (o usar -var en la linea de comandos)
task infra:apply TARGET=module.batch EXTRA="-var=trainer_image_tag=v0.2.0"
```

## 5.6 Verificar end-to-end

```bash
# Re-correr smoke con el trainer parchado
task batch:smoke

# Confirmar metric publicada
aws cloudwatch list-metrics 
    --namespace "ml-training/Training" 
    --metric-name MAPE 
    --dimensions Name=variety,Value=POP 
    --query 'Metrics[]' --output table

# Y traer el ultimo data point
aws cloudwatch get-metric-statistics 
    --namespace "ml-training/Training" 
    --metric-name MAPE 
    --dimensions Name=variety,Value=POP 
    --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
    --end-time   "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --period 60 
    --statistics Maximum 
    --query 'Datapoints'
```

Si trae un valor, **la alarma `ml-training-mape-pop` ya tiene datos**
y va a dispararse cuando supere `mape_alarm_threshold` (default 25%).

---

# Parte 6 — CI/CD con GitHub Actions

## 6.0 Modelo de trust (resumen)

Recordatorio: en Parte 2.5 creaste el OIDC provider, y en Parte 3.11 el
modulo `cicd` creo 2 roles que confian en ese provider para tu
`org/repo`:

- `ml-training-gha-deploy` — usado por workflows que tocan infra
  (terraform apply, push ECR, secrets).
- `ml-training-gha-train` — usado por workflows que solo invocan
  Lambda dispatcher.

Los ARNs estan en los outputs de Terraform:

```bash
terraform -chdir=infra/envs/prod output gha_deploy_role_arn
terraform -chdir=infra/envs/prod output gha_train_role_arn
```

## 6.0.1 Filosofia: Task = single source of truth

> **Por que esta seccion existe.** Es la diferencia mas grande entre la V1
> de esta guia y la V2. La V1 tenia toda la logica de CI duplicada dentro
> de los `.yml` de GitHub Actions (pip install, terraform init con backend
> config, docker build con tags, etc.). La V2 mueve **toda** esa logica
> a `Taskfile.yml` + `tasks/*.yml` y los workflows se vuelven "thin":
> autentican via OIDC, setean `TF_VAR_*` y llaman `task X`.

### La regla

```
Si `task X` corre OK en tu laptop → corre OK en CI.
Si rompe en CI → rompe en local con `task X`.
```

Esto se logra porque **solo existe UNA implementacion** de cada operacion
(la que vive en Taskfile). Los workflows son envoltorios delgados.

### Anti-patron que evita la V2

```yaml
# V1: logica duplicada entre ci.yml y Taskfile
# .github/workflows/ci.yml
- run: pip install -r requirements.txt
- run: ruff check src/ main.py scripts/
- run: terraform fmt -check -recursive infra/

# Taskfile.yml (otro lugar, misma logica)
tasks:
  lint:
    cmds:
      - ruff check src/ main.py scripts/
```

**Problema**: si manana agregas `mypy`, hay que tocar 2 archivos. Y peor:
un dev que corre `task lint` local NO ve el `terraform fmt` que CI si
corre → el PR se rompe en CI pero "funcionaba en local".

### El patron correcto (V2)

```yaml
# V2: workflow llama a task, task es SoT
# .github/workflows/ci.yml
- uses: arduino/setup-task@v2
- run: task lint           # ← misma cosa que el dev corre local
- run: task test
- run: task infra:validate

# Taskfile.yml
tasks:
  lint:
    cmds:
      - ruff check src/ main.py scripts/
```

Cambias el linter en `Taskfile.yml` → CI lo hereda en el siguiente run.
Single source of truth.

### 6.0.1.1 Matriz de decision: Task vs Terraform vs GHA vs Lambda

Cuando agregues una operacion nueva, esta tabla te dice **donde vive la
implementacion** (no donde se dispara — eso es siempre GHA workflows).

| Operacion | Herramienta | Por que |
|---|---|---|
| Crear VPC, RDS, Batch queue, Lambda, alarmas | **Terraform** (`infra/modules/*`) | Declarativo, idempotente, state |
| Build + push imagen a ECR | **Task** (`task ecr:build-all`) | Imperativo, encadena `docker build` + ECR login + tag dual `latest`/`sha-XXX` |
| Submit Batch job + polling hasta SUCCEEDED | **Task** (`task batch:train`) | Encadena `lambda invoke` + `jq` + `aws batch describe-jobs` con loop |
| Apply Terraform en oleadas + smoke | **Task** (`task aws:deploy`) | Orquesta oleadas A+B+C (storage → 3 imagenes → resto) |
| Wake / sleep RDS+Fargate | **Task** (`task aws:wake`/`aws:sleep`) | `scale-up` + `sleep 300` + `wait-healthy` — Terraform no hace polling, Lambda scheduler si pero esto es operacion manual |
| Disparar un entreno bajo demanda | **GHA workflow_dispatch** (`train.yml`) | UI con dropdown de variedades + tuning, accesible desde el browser sin AWS CLI |
| Approval humano antes de deploy o promote | **GHA `environment: production`** | Solo GHA tiene approval gates con reviewers |
| Cron L-V 08-12 PET para encender/apagar | **EventBridge + Lambda** (`infra/lambdas/scheduler.py`) | Serverless, sin runner; corre aunque no haya devs |
| Quality gate MAPE + A/B contra Production | **Task** (`task mlflow-aws:promote`) llamado por GHA | Logica Python compleja, reutilizable local (un dev puede correr `task mlflow-aws:promote VARIETY=POP VERSION=5` para validar antes del workflow) |
| Apagar todo con confirmacion textual | **GHA workflow** (`destroy.yml`) + **Task** (`task aws:teardown` / `aws:destroy`) | Workflow porque la confirmacion textual `DESTRUIR-ML-TRAINING` + approval son features de GHA; pero la **logica** del destroy vive en Task |

### 6.0.1.2 Excepciones (cuando NO meter en Taskfile)

Tres anti-patrones que evitar:

| Anti-patron | Ejemplo | Donde va en su lugar |
|---|---|---|
| **Task que solo corre en CI** | `gha:upload-artifact` (artifacts son feature de GHA) | Step inline en el workflow |
| **Task wrapper trivial de 1 linea** | `docker:build` que es solo `docker build .` | Eliminar (usar `docker build .` directo) |
| **Logica que deberia ser Terraform** | `infra:create-vpc` con `aws ec2 create-vpc ...` | Modulo Terraform (`infra/modules/network/main.tf`) |

> **Regla concreta**: si un task no (a) encadena >=2 cosas, (b) pasa
> variables computadas (`{{.SUFFIX}}` resuelto via `aws sts`), o
> (c) agrega idempotencia/wait/polling — entonces no justifica el namespace
> y mejor va inline.

### 6.0.1.3 Donde se ve esto en el repo

```
ml_training/
├── Taskfile.yml                       ← root: tasks locales (build, train, lint, test)
├── tasks/
│   ├── infra.yml                      ← namespace `infra:` (terraform wrapper)
│   ├── ecr.yml                        ← namespace `ecr:` (build + push 3 imagenes)
│   ├── batch.yml                      ← namespace `batch:` (submit + tail jobs)
│   ├── cluster.yml                    ← namespace `cluster:` (scale-up/down RDS+Fargate)
│   ├── mlflow_registry.yml            ← namespace `mlflow-aws:` (promote modelos)
│   └── aws.yml                        ← namespace `aws:` (orquestador macro: deploy/wake/sleep/teardown/destroy)
└── .github/workflows/
    ├── deploy.yml                     ← consolida ci + terraform-plan + infra-apply (4 jobs: changes, lint-and-test, build-and-push, terraform-plan, infra-apply). Llama `task lint`, `task test`, `task infra:validate`, `task ecr:build`, `task infra:plan`, `task aws:deploy`.
    ├── training.yml                   ← consolida train + auto-train + promote (jobs: detect, wake-services, train, cool-down-and-stop, promote). Llama `task batch:train-lambda` (mismo path que el dispatcher Lambda) y `task mlflow-aws:promote`.
    └── destroy.yml                    ← 3 modos: `task aws:teardown` (TEAR-DOWN) | `task aws:destroy` (DESTROY) | `task aws:nuke` (NUKE).
```

Total: **~670 lineas de Taskfile + tasks/** vs **~645 lineas de los 3
workflows consolidados** (deploy 244 + training 245 + destroy 156). La
V1 tenia 6 workflows gordos con logica inline (~1500 lineas) y casi
nada en Task — la V2 redistribuye: los workflows son thin shells que
solo orquestan triggers/permisos/concurrency, y la logica real vive
en Task (ejecutable identico desde laptop o CI).

## 6.1 Variables y secrets de GitHub

Settings → Secrets and variables → Actions.

**Variables** (no secret, visibles en logs):

| Nombre | Valor |
|---|---|
| `AWS_REGION` | `us-east-1` |
| `AWS_ACCOUNT_ID` | tu account id 12 digitos |
| `AWS_GHA_DEPLOY_ROLE_ARN` | `arn:aws:iam::<account>:role/ml-training-gha-deploy` |
| `AWS_GHA_TRAIN_ROLE_ARN` | `arn:aws:iam::<account>:role/ml-training-gha-train` |
| `ECR_TRAINER` | `<account>.dkr.ecr.us-east-1.amazonaws.com/ml-training` |
| `MLFLOW_ALB_DNS` | (output de terraform: `alb_dns`) |
| `PROJECT` | `ml-training` |
| `ALERT_EMAIL` | email para notificaciones SNS (ej. `abantodca@gmail.com`). La usan `deploy.yml` y `destroy.yml` como `TF_VAR_alert_email`. |
| `CONSUMER_ORG` | org del consumer repo (ej. `abantodca`). Se inyecta como `TF_VAR_consumer_org` y autoriza al repo consumidor (`ml_serving`) a asumir el rol cross-repo de lectura del Model Registry. |
| `CONSUMER_REPO` | nombre del consumer repo (ej. `ml_serving`). Se inyecta como `TF_VAR_consumer_repo`. Junto con `CONSUMER_ORG` arma el subject `repo:<org>/<repo>:*` del trust policy. |

**Secrets**: ninguno (OIDC remplaza el caso comun de access keys). Si
en algun workflow necesitas un Slack webhook u otro secret externo,
ahi si va en secrets.

### Setearlas via gh CLI

```bash
gh variable set AWS_REGION              -b "us-east-1"
gh variable set AWS_ACCOUNT_ID          -b $ACCOUNT_ID
gh variable set AWS_GHA_DEPLOY_ROLE_ARN -b "$(terraform -chdir=infra/envs/prod output -raw gha_deploy_role_arn)"
gh variable set AWS_GHA_TRAIN_ROLE_ARN  -b "$(terraform -chdir=infra/envs/prod output -raw gha_train_role_arn)"
gh variable set ECR_TRAINER             -b "$ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/ml-training"
gh variable set MLFLOW_ALB_DNS          -b "$(terraform -chdir=infra/envs/prod output -raw alb_dns)"
gh variable set PROJECT                 -b "ml-training"
gh variable set ALERT_EMAIL             -b "abantodca@gmail.com"
gh variable set CONSUMER_ORG            -b "abantodca"
gh variable set CONSUMER_REPO           -b "ml_serving"
```

> **Importante**: `ALERT_EMAIL`, `CONSUMER_ORG` y `CONSUMER_REPO` deben
> estar seteadas **antes** del primer run del job `infra-apply` de
> `deploy.yml`, sino el step `task aws:deploy` falla con
> `variable "alert_email"/"consumer_org"/"consumer_repo" required value not provided`.

## 6.2 `.github/workflows/deploy.yml` — consolidado (lint/test/build/plan/apply)

> **Por que esta seccion existe**. La V1 tenia 3 workflows separados
> (`ci.yml`, `terraform-plan.yml`, `infra-apply.yml`) que repetian
> checkout, setup-python, configure-aws-credentials, etc. — y obligaban
> a mantener 3 archivos sincronizados cada vez que cambiabas la version
> de Terraform o el rol OIDC. La V2 los consolida en un solo `deploy.yml`
> con 5 jobs condicionales que comparten estado a traves del job
> `changes` (`dorny/paths-filter@v3`).

Trigger: push a `main`, PR a `main`, o `workflow_dispatch`. Cinco jobs:

| Job | Disparo | Que hace |
|---|---|---|
| `changes` | siempre | `dorny/paths-filter@v3` setea outputs `infra` (si toco `infra/**` o el workflow) y `trainer` (si toco `src/**`, `main.py`, `Dockerfile`, `requirements.txt`). |
| `lint-and-test` | siempre | Mismo job que el V1 `ci.yml`: instala deps y corre `task lint && task test && task infra:validate`. |
| `build-and-push` | push a `main` **AND** `trainer == 'true'` | Asume `gha-deploy`, llama `task ecr:build IMG=trainer` (build + push a ECR con tag `latest` + `sha-<sha>`). |
| `terraform-plan` | PR **AND** `infra == 'true'` | Asume `gha-deploy`, corre `task infra:plan` redirigido a `tfplan.txt`, y postea el output (truncado a 60 KB) como comment en el PR via `actions/github-script@v7`. |
| `infra-apply` | push a `main` **AND** `infra == 'true'` **AND** `lint-and-test == success` **AND** `build-and-push in (success, skipped)` | `environment: production` (approval manual). Llama `task aws:deploy` con `TF_VAR_trainer_image_tag=sha-<sha>` para pinear el trainer recien construido. Publica URLs en el summary. |

**El archivo real vive en `/home/cabanto/proyectos/ml_random_forest/ml_training/.github/workflows/deploy.yml`** — 244 lineas con todos los `steps` detallados, env vars (`TF_VAR_consumer_org`, `TF_VAR_consumer_repo`, `TF_VAR_alert_email`, `TF_VAR_github_org`, `TF_VAR_github_repo`, `TF_VAR_trainer_image_tag`) e `if:` por job. Esta seccion explica el "que/por que"; el "como exacto" esta versionado en el repo.

> **Aplicacion del patron 6.0.1**: este workflow es **thin** — cada
> step pesado (lint, test, validate, build, plan, apply) es una
> llamada a `task X`. La logica vive en `tasks/*.yml` (§4.1) y corre
> identica desde tu laptop. Si queres validar antes de pushear:
> `task lint && task test && task infra:validate && task infra:plan`.

### 6.2.1 Esqueleto del workflow

Lo que sigue es el **esqueleto** (triggers + nombres de jobs + `if:`),
no los steps internos. Para el contenido completo, ver el archivo real.

```yaml
name: Deploy

on:
  push:        { branches: [main] }
  pull_request: { branches: [main] }
  workflow_dispatch: {}

permissions:
  id-token: write
  contents: read
  pull-requests: write     # necesario para que terraform-plan comente en PRs

concurrency:
  group: deploy-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

jobs:
  changes:                # dorny/paths-filter@v3 -> outputs.infra / outputs.trainer
  lint-and-test:          # siempre
  build-and-push:
    needs: [lint-and-test, changes]
    if: github.event_name == 'push' && github.ref == 'refs/heads/main' && needs.changes.outputs.trainer == 'true'
  terraform-plan:
    needs: [lint-and-test, changes]
    if: github.event_name == 'pull_request' && needs.changes.outputs.infra == 'true'
  infra-apply:
    needs: [lint-and-test, changes, build-and-push]
    environment: production
    if: |
      always() &&
      github.event_name == 'push' && github.ref == 'refs/heads/main' &&
      needs.lint-and-test.result == 'success' &&
      (needs.build-and-push.result == 'success' || needs.build-and-push.result == 'skipped') &&
      needs.changes.outputs.infra == 'true'
```

> **Por que `if: always()` en `infra-apply`**: el job depende de
> `build-and-push`, pero ese job puede haber sido **skipped** (si el
> push no toco el trainer). Sin `always()`, el motor de GHA marca
> `infra-apply` como `skipped` automaticamente. Con `always()` + el
> check explicito `(success || skipped)` en el `if:`, el apply corre
> aunque solo cambie infra sin tocar el trainer.

> **Por que `cancel-in-progress` es condicional**: en PRs queremos
> cancelar runs viejos cuando se hacen force-push (ahorra CI minutes).
> En push a main NO queremos cancelar — un apply a medias deja state
> inconsistente. El `if: github.event_name == 'pull_request'` resuelve
> ambos casos sin necesidad de dos workflows separados.

> **Inyeccion de consumer org/repo**: los jobs `terraform-plan` e
> `infra-apply` exportan `TF_VAR_consumer_org` y `TF_VAR_consumer_repo`
> desde `vars.CONSUMER_ORG` y `vars.CONSUMER_REPO` (§6.1). Sin esas
> dos vars seteadas en GitHub, Terraform falla con
> `variable "consumer_org" required value not provided` y el deploy
> aborta antes de tocar AWS. El subject final del trust cross-repo
> (`repo:<org>/<repo>:*`) se arma en el modulo `cicd` (§3.11).

> **Equivalente en AWS Console — que ve cada job en AWS**:
>
> | Job | AWS Console | Que aparece |
> |---|---|---|
> | `build-and-push` (push main) | **ECR > Repositories > ml-training > Images** | Imagen nueva con 2 tags (`latest` + `sha-abc123def456`). En CloudTrail: `STS:AssumeRoleWithWebIdentity` + `GetAuthorizationToken` + `PutImage`. |
> | `terraform-plan` (PR) | **CloudTrail > Event history** | Rafaga de `DescribeXxx` (read-only). Y un `GetObject` sobre `s3://ml-training-tfstate-*/envs/prod/terraform.tfstate` + `GetItem` sobre la tabla DynamoDB `ml-training-tflock`. **No modifica nada**. |
> | `infra-apply` (push main, approved) | **VPC + RDS + ECS + Batch + λ Lambda** | El despliegue completo (oleadas A+B+C de `task aws:deploy`). Tipicamente 60-90 recursos creados/modificados, mas las 3 imagenes pusheadas en oleada B (`ml-training`, `ml-training-mlflow`, `ml-training-reports`). |
> | Approval `environment: production` | **GitHub > Actions > run > "Waiting for review"** | El reviewer recibe email + ve "Review deployments" en la UI. Hasta que clickees "Approve and deploy", el job NO arranca — es la salvaguarda humana antes de tocar prod. |

> **Configuracion previa en GitHub** (UNA sola vez):
>
> 1. **Settings → Environments → New environment → `production`**
> 2. Marca **Required reviewers** → agregate
> 3. (Opcional) **Deployment branches and tags** → "Selected branches" → `main`
>
> Sin esto, el job `infra-apply` arranca sin approval y derrota el
> proposito de la gate.

> **Checkpoint despues de 6.2**: hace un push trivial a `main` que NO
> toque ni `infra/**` ni el trainer (e.g., editar este `.md`) y valida
> que solo corre `lint-and-test`:
>
> ```bash
> git commit --allow-empty -m "test: trigger deploy.yml"
> git push origin main
> gh run list --workflow=deploy.yml --limit 1
> # Esperado: status=completed, conclusion=success
> # En la UI: solo lint-and-test verde. build-and-push, terraform-plan, infra-apply skipped.
> ```
>
> Para validar `infra-apply` end-to-end, dispara el workflow manualmente
> via `gh workflow run deploy.yml` desde `main` (corre lint + apply con
> approval). Si todo OK, ves los outputs (`alb_dns`, `tracking_uri`,
> ...) en el summary.

## 6.3 `terraform-plan.yml` (eliminado)

> **§6.3 (terraform-plan.yml) eliminado**: ahora es un job dentro de
> `deploy.yml` (`terraform-plan`) que solo corre en PRs con cambios a
> `infra/**`. Comenta el output del plan en el PR via
> `actions/github-script@v7`. La logica que antes vivia aqui
> (terraform init/validate/plan + comment al PR) ahora vive en
> `task infra:plan` + el step `Comentar plan en el PR` del workflow
> consolidado. Ver §6.2.

## 6.3.5 `infra-apply.yml` (eliminado)

> **§6.3.5 (infra-apply.yml) eliminado**: ahora es un job dentro de
> `deploy.yml` (`infra-apply`) que solo corre en push a `main` con
> cambios a `infra/**` y requiere approval del environment
> `production`. Llama `task aws:deploy` con el mismo
> `TF_VAR_trainer_image_tag=sha-<sha>` que pinea la imagen recien
> pusheada por el job `build-and-push`. Ver §6.2.

## 6.4 `.github/workflows/training.yml` — consolidado (train + auto-train + promote)

> **Por que esta seccion existe**. La V1 tenia 3 workflows
> superpuestos: `train.yml` (manual desde la UI), `auto-train-on-push.yml`
> (auto-train cuando un push tocaba el trainer) y `promote.yml`
> (transition a Production con gates). Los 3 compartian wake/cool-down,
> autenticacion OIDC y conocimiento del Registry — y se desincronizaban
> facil. La V2 los consolida en un solo `training.yml` con un job
> `detect` que decide el modo segun el trigger.

Trigger: `workflow_dispatch` (UI manual) **o** `workflow_run` cuando
`Deploy` completa con success (auto-train). Cinco jobs:

| Job | Disparo | Que hace |
|---|---|---|
| `detect` | siempre | Decide el modo. Si `workflow_dispatch`: usa `inputs.action` (`train` o `promote`). Si `workflow_run`: hace `git diff` entre el SHA del push y el anterior — si toco `src/**`, `main.py` o `Dockerfile`, setea `mode=train` con `varieties=all, tuning=prod`; si no, `mode=skip`. |
| `wake-services` | `mode == 'train'` | Asume `gha-train`, hace `curl /health` al ALB de MLflow. Si DOWN, invoca `${PROJECT}-scheduler` Lambda con `{"action":"start"}` y espera hasta 12 min a que RDS este `available` y 5 min a que MLflow responda 200. Outputs `mlflow_was_up` (`true`/`false`) para que cool-down sepa si apagar. |
| `train` | `mode == 'train'` | Asume `gha-train`, llama `task batch:train-lambda VARIETIES=... TUNING=... WAIT=true`. **Importante**: usa el MISMO path que el dispatcher Lambda invoca — no hace `aws batch submit-job` directo. Asi un dev local con `task batch:train-lambda` ejecuta IDENTICO el camino. |
| `cool-down-and-stop` | `mode == 'train'` **AND** `wake-services.outputs.mlflow_was_up == 'false'` | Espera 10 min (cool-down para sync S3 + dashboards), luego invoca `${PROJECT}-scheduler` con `{"action":"stop"}`. **Solo apaga si nosotros lo levantamos** — si MLflow ya estaba UP, no lo apaga. |
| `promote` | `mode == 'promote'` | `environment: production` (approval manual). Asume `gha-train`, llama `task mlflow-aws:promote MODEL_NAME=... VERSION=... MAX_MAPE=...`. Los 3 gates (MAPE absoluto + A/B vs Production actual + transition con `archive_existing_versions=True`) viven adentro de la task — corre identico en local. |

**El archivo real vive en `/home/cabanto/proyectos/ml_random_forest/ml_training/.github/workflows/training.yml`** — 245 lineas con todos los `steps`, los polling loops de RDS+ALB, el `git diff` del detect, y los env vars. Esta seccion explica el "que/por que".

> **Aplicacion del patron 6.0.1**: el job `train` es **literalmente
> una linea**: `task batch:train-lambda VARIETIES=... TUNING=...
> WAIT=true`. La logica de submitir via dispatcher Lambda + polling +
> log streaming vive en `tasks/batch.yml`. El job `promote` es
> similar: `task mlflow-aws:promote ...` — los 3 gates viven en
> `tasks/mlflow_registry.yml`. Sin esto, los workflows V1 duplicaban
> la logica en Python embebido y se rompian en cada bump de mlflow.

### 6.4.1 Esqueleto del workflow

```yaml
name: Training

on:
  workflow_dispatch:
    inputs:
      action:     { type: choice, options: [train, promote], default: train }
      # inputs de action=train (se ignoran si action=promote)
      varieties:  { type: string, default: 'all' }
      tuning:     { type: choice, options: [smoke, dev, prod, prod_xl], default: prod }
      # inputs de action=promote (se ignoran si action=train)
      model_name: { type: string }
      version:    { type: string }
      max_mape:   { type: string, default: '20' }

  workflow_run:                       # auto-train post-deploy
    workflows: ["Deploy"]
    types: [completed]
    branches: [main]

permissions:
  id-token: write
  contents: read

concurrency:
  group: training-${{ github.event.inputs.action || 'auto' }}
  cancel-in-progress: false

jobs:
  detect:                             # decide mode=train|promote|skip
  wake-services:                      # if: mode == 'train'  -> outputs.mlflow_was_up
  train:                              # if: mode == 'train'  -> task batch:train-lambda
  cool-down-and-stop:                 # if: mode == 'train' && was_up == 'false' -> scheduler.stop
  promote:                            # if: mode == 'promote' -> task mlflow-aws:promote + env=production
```

> **Por que el job `train` NO hace `aws batch submit-job` directo**:
> seguridad. El rol `gha-train` SOLO tiene `lambda:InvokeFunction` sobre
> `${PROJECT}-dispatcher` (y `scheduler`). Si el job hiciera submit
> directo, el rol necesitaria `batch:SubmitJob` con resource `*` (no
> hay forma de scopear a una job-def especifica con overrides). Con
> el dispatcher en el medio, la validacion de payload (varieties
> whitelist, tuning whitelist, s3 key regex) vive en Lambda (§5.x).

> **Por que `workflow_run on Deploy success`**: cuando un push a
> main toca el trainer, `Deploy` corre `build-and-push` (nueva imagen
> en ECR) + `infra-apply` (apunta job-def a `sha-<commit>`). Si esa
> cadena fue exitosa, queremos auto-entrenar para validar la nueva
> imagen. El `detect` filtra: solo dispara si el push toco
> `src/**|main.py|Dockerfile`. Cambios solo a `infra/**` o docs NO
> disparan auto-train (no cambia comportamiento del modelo).

> **Por que cool-down condicional (`was_up == 'false'`)**: si un
> humano ya prendio MLflow para mirar dashboards y dispara un train
> manual desde la UI, NO queremos que el workflow lo apague al
> terminar. Solo apaga si nosotros lo prendimos. El output
> `mlflow_was_up` del job `wake-services` es la senal.

> **Equivalente en AWS Console — la cadena de eventos**:
>
> | Job | AWS Console | Que aparece |
> |---|---|---|
> | `wake-services` (si DOWN) | **λ Lambda > Functions > ml-training-scheduler > Monitor** | Invocacion con payload `{"action":"start"}`. CloudTrail muestra el resultado: `modify-db-instance` (RDS) + `update-service` (ECS Fargate). |
> | (durante wake polling) | **RDS > Databases > ml-training-mlflow** | Status: `starting` -> `available` (~5-8 min). El workflow hace polling cada 30s hasta 12 min. |
> | `train` (`task batch:train-lambda`) | **λ Lambda > ml-training-dispatcher > Monitor > Recent invocations** | Aparece invocacion nueva con input `{"varieties":"...","tuning":"..."}` y output con `jobId`. |
> | `train` (durante run) | **Batch > Jobs** | Job pasa `SUBMITTED -> RUNNABLE -> STARTING -> RUNNING -> SUCCEEDED`. EC2 Spot c6i.2xlarge se levanta y se apaga sola en ~5 min post-job. |
> | `cool-down-and-stop` (si was_up=false) | **λ Lambda > ml-training-scheduler** | Invocacion con `{"action":"stop"}`. Tras eso: RDS `stopping`, Fargate services `desiredCount=0`. |
> | `promote` | **ALB > MLflow UI > Models > ml-training-POP** | Badge "Production" se mueve a la version nueva. La anterior va a Archived. RDS recibe UPDATE sobre tabla `model_versions`. |

Uso desde la UI:

1. Actions → Training → Run workflow
2. **Para entrenar**: `action=train`, `varieties=POP` (o `all`),
   `tuning=prod` (o `smoke` para validar end-to-end rapido).
3. **Para promover**: `action=promote`, `model_name=rnd-forest-POP`,
   `version=5`, `max_mape=20`. Espera el "Waiting for review" y aproba.

> **Checkpoint despues de 6.4**: corre un smoke training y verifica
> que el dispatcher recibe el invoke:
>
> ```bash
> gh workflow run training.yml -f action=train -f varieties=POP -f tuning=smoke
> gh run list --workflow=training.yml --limit 1
> # Esperado: wake-services (skip si MLflow up) -> train (SUCCEEDED) -> cool-down (skip si was_up)
> aws logs tail /aws/lambda/ml-training-dispatcher --since 5m
> # Esperado: log con SubmitJob OK + jobId del Batch
> ```

## 6.5 `promote.yml` (eliminado)

> **§6.5 (promote.yml) eliminado**: ahora es un job dentro de
> `training.yml` (`promote`, disparado con `inputs.action=promote`).
> Usa `task mlflow-aws:promote MODEL_NAME=... VERSION=...
> MAX_MAPE=...` que aplica los 3 gates (MAPE absoluto + A/B vs
> Production actual + transition con `archive_existing_versions=True`)
> identicos a la version V1 — pero ahora la logica vive en
> `tasks/mlflow_registry.yml` y corre identica en local con
> `task mlflow-aws:promote ...`. El `environment: production` sigue
> requiriendo approval manual. Ver §6.4.

## 6.5.5 `.github/workflows/destroy.yml` — 3 modos (TEAR-DOWN / DESTROY / NUKE)

> **Por que esta seccion existe**. Los modos de apagado de
> Parte 1 son operaciones de runbook que la V1 corria a mano via
> `terraform destroy`. Eso tiene dos problemas: (a) si tu laptop no
> tiene credenciales o estas en vacaciones, nadie puede apagar la
> infra (sigue facturando), y (b) un `terraform destroy` accidental
> en la terminal equivocada borra prod sin manera de revertirlo.
> `destroy.yml` resuelve ambos: corre desde GitHub UI con **doble
> salvaguarda** y ahora soporta **3 modos** segun cuanto querras
> destruir.

Trigger: **solo `workflow_dispatch`** (nunca automatico). Inputs:

- `confirmar`: texto literal `DESTRUIR-ML-TRAINING` (sin match exacto el job se skipea).
- `modo`: choice — `TEAR-DOWN` | `DESTROY` | `NUKE`.

| Modo | Que destruye | Que preserva | Reversible con |
|---|---|---|---|
| **TEAR-DOWN** | Modulos volatiles: `mlflow`, `reports`, `batch`, `lambdas`, `monitoring`, `scheduler`. ECS services a 0, Batch compute envs a 0, RDS detenido. | **S3** (artifacts + data + tfstate), **ECR** (las 3 imagenes), **network** (VPC/subnets/NAT), **OIDC provider**, **modulo `cicd`** (roles + variables). | `task aws:rebuild` (~20 min, modelos intactos). Costo restante: ~$8/mes (S3). |
| **DESTROY** | `terraform destroy` de **TODOS** los modulos administrados (incluido `storage`). Borra buckets de artifacts/data + ECR repos + RDS + VPC + ALB. La task `aws:destroy` ahora vacia buckets con versioning y purga repos ECR ANTES del `terraform destroy` (sino TF falla con `BucketNotEmpty` / `RepositoryHasImages`). | **tfstate bucket** + **tflock DynamoDB** + **OIDC provider** (los recursos de bootstrap). | Re-crear todo via `task aws:deploy` (oleadas A+B+C). Costo: $0/mes. |
| **NUKE** | **DESTROY** + borra el tfstate bucket + tflock DynamoDB + OIDC provider de IAM. Limpieza total de la cuenta. | Nada del proyecto. | Necesitas re-bootstrap desde cero (Parte 2). |

**El archivo real vive en `/home/cabanto/proyectos/ml_random_forest/ml_training/.github/workflows/destroy.yml`** — 156 lineas con la doble salvaguarda, los 3 steps condicionales (`if: ${{ inputs.modo == 'X' }}`), el cleanup de log groups de Lambda (solo si modo != TEAR-DOWN), y la verificacion post-operacion (EC2 + RDS + Fargate + ALB + ECR + S3 + OIDC provider).

> **Aplicacion del patron 6.0.1**: cada modo es **una sola linea**:
> `task --yes aws:teardown`, `task --yes aws:destroy`, `task --yes aws:nuke`.
> El `--yes` auto-confirma el `prompt:` interactivo del Taskfile (que
> sigue valido en local cuando corres la task desde tu laptop).

### 6.5.5.1 Doble salvaguarda contra accidentes

```
┌─ Salvaguarda 1: input textual ────────────────────────────┐
│                                                           │
│  El campo "confirmar" requiere escribir literalmente:     │
│                                                           │
│     DESTRUIR-ML-TRAINING                                  │
│                                                           │
│  El `if:` del job matchea string-exacto. Si escribes      │
│  "DESTRUIR-ml-training" (lowercase) o "DESTRUIR" o        │
│  cualquier variacion, el job termina con conclusion=      │
│  skipped y NO ejecuta destroy.                            │
└───────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─ Salvaguarda 2: environment approval ─────────────────────┐
│                                                           │
│  `environment: production` requiere que un reviewer       │
│  (vos) clickee "Approve and deploy" en la UI antes de     │
│  que el job arranque. Si te equivocaste de modo, tenes    │
│  esta ultima ventana para Cancelar.                       │
└───────────────────────────────────────────────────────────┘
```

### 6.5.5.2 Esqueleto del workflow

```yaml
name: Destroy

on:
  workflow_dispatch:
    inputs:
      confirmar:
        description: 'Escribe exactamente: DESTRUIR-ML-TRAINING'
        required: true
      modo:
        type: choice
        options: [TEAR-DOWN, DESTROY, NUKE]
        default: TEAR-DOWN

permissions: { id-token: write, contents: read }

jobs:
  destroy:
    if: ${{ inputs.confirmar == 'DESTRUIR-ML-TRAINING' }}
    environment: production
    steps:
      - uses: actions/checkout@v4
      - name: Assume gha-deploy
        # ...
      - name: TEAR-DOWN
        if: ${{ inputs.modo == 'TEAR-DOWN' }}
        run: task --yes aws:teardown
      - name: DESTROY
        if: ${{ inputs.modo == 'DESTROY' }}
        run: task --yes aws:destroy
      - name: NUKE
        if: ${{ inputs.modo == 'NUKE' }}
        run: task --yes aws:nuke
      - name: Limpiar log groups Lambda (modo != TEAR-DOWN)
        # ...
      - name: Verificacion post-operacion (lista lo que sobrevive)
        # ...
```

> **Equivalente en AWS Console — que pasa adentro segun el modo**:
>
> | Modo | Recursos visibles despues |
> |---|---|
> | **TEAR-DOWN** | S3 buckets ml-training-* siguen. ECR repos siguen con imagenes. VPC/Subnets/NAT siguen. RDS `ml-training-mlflow` con final snapshot. Fargate services con `desiredCount=0`. Batch compute env con `desiredvCpus=0`. |
> | **DESTROY** | S3 tfstate bucket + DynamoDB tflock + OIDC provider. Todo lo demas borrado. CloudWatch log groups de Lambdas limpiados explicitamente (sino sobreviven). |
> | **NUKE** | Nada del proyecto. Cuenta limpia. |

> **Por que 3 modos y no 2**: la V1 solo tenia TEAR-DOWN y
> DESTROY. Pero DESTROY no borraba el tfstate ni el OIDC provider
> (los modulos de bootstrap), asi que la cuenta quedaba con residuos.
> Para escenarios de "cierre del proyecto" o "migracion de cuenta",
> NUKE es el barrido final. Para el dia a dia (sprint terminado,
> ahorrar costo de fin de semana), TEAR-DOWN sigue siendo el modo
> del 90% de las veces.

> **Errores tipicos del destroy**:
>
> | Sintoma | Causa | Solucion |
> |---|---|---|
> | `BucketNotEmpty` | Bucket S3 con versioning + objetos no vaciados | `task aws:destroy` ahora vacia con `--all-versions` antes del TF destroy. Si persiste, re-correr el workflow. |
> | `RepositoryHasImages` (ECR) | ECR repo con imagenes y `force_delete=false` en TF | `task aws:destroy` purga con `aws ecr batch-delete-image` antes del TF destroy. Si persiste, verificar `force_delete=true` en `infra/modules/storage`. |
> | `DependencyViolation: Network interface ... is currently in use` | Lambdas con ENIs activos. AWS los libera lento (5-15 min) | Esperar 15 min y re-correr. |
> | Workflow no arranca, status=skipped | Texto de `confirmar` no matcheo exacto | Re-disparar con `DESTRUIR-ML-TRAINING` (case-sensitive, sin espacios). |
> | Elegiste modo equivocado | Dropdown mal seleccionado | Cancelar antes del approval y re-disparar con el modo correcto. |

> **Checkpoint despues de 6.5.5**: dispara el workflow en modo
> TEAR-DOWN como dry-run de la confirmacion textual:
>
> ```bash
> # Primero confirma que el `if:` filtra:
> gh workflow run destroy.yml -f confirmar=destruir -f modo=TEAR-DOWN
> gh run list --workflow=destroy.yml --limit 1
> # Esperado: conclusion=skipped (texto no matchea exacto)
>
> # Ahora con el texto correcto:
> gh workflow run destroy.yml -f confirmar=DESTRUIR-ML-TRAINING -f modo=TEAR-DOWN
> gh run watch
> # Esperado: "Waiting for review" -> approve -> task --yes aws:teardown
> # Final: Fargate desiredCount=0, S3 + ECR + Registry intactos
> ```
>
> **NUNCA pruebes DESTROY o NUKE la primera vez** salvo que ya hayas
> terminado el proyecto. DESTROY es irreversible para artifacts/RDS;
> NUKE adicionalmente exige re-bootstrap completo.

## 6.6 Branch protection

El required status check cambio de nombre con la consolidacion: antes
era `lint-and-test` (job del viejo `ci.yml`); ahora es `Deploy /
lint-and-test` (el mismo job, pero ya vive dentro del workflow
consolidado `deploy.yml` con `name: Deploy`).

```bash
# Required status checks: el job lint-and-test del deploy.yml consolidado
gh api "repos/${GITHUB_OWNER}/ml_training/branches/main/protection" -X PUT --input - <<EOF
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["Deploy / lint-and-test"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1
  },
  "restrictions": null
}
EOF
```

> **Por que el formato `<Workflow Name> / <Job ID>`**: GitHub
> identifica status checks por el par `(workflow display name, job
> id)`. El workflow tiene `name: Deploy` y el job tiene id
> `lint-and-test`, asi que el check aparece como `Deploy /
> lint-and-test` en la API de branch protection y en el dropdown de
> Settings.

(O configurar via GitHub UI: Settings → Branches → Branch protection
rules → Add rule → Require status checks to pass → buscar y agregar
`Deploy / lint-and-test`.)

---

# Parte 7 — Promotion gate (extendido)

La Parte 6.5 ya implementa el gate basico (MAPE umbral + A/B + approval).
Esta Parte 7 documenta el ciclo completo del modelo y cuando se promueve.

## 7.1 Ciclo de vida de un modelo

```
[push a main]
       │
       ▼
ci.yml: build + push :sha-abc123 a ECR
       │
       ▼
[manual trigger train.yml en GitHub UI]
       │
       ▼
Lambda dispatcher -> Batch -> trainer corre con :sha-abc123
       │
       ▼
trainer loguea run a MLflow + custom metric MAPE a CloudWatch
       │
       ▼
trainer registra modelo en Registry stage "None"
       │
       ▼
[review manual: mirar dashboard /reports/<variety>/]
       │
       ▼
[transition a Staging via UI MLflow o API]
       │
       ▼
[manual trigger promote.yml en GitHub UI]
       │
       ├─> Gate 1: MAPE < max_mape?  [si NO -> abort]
       ├─> Gate 2: mejora vs Production actual? [si NO -> abort]
       ├─> Gate 3: approval humano (GitHub Environment)
       │
       ▼
transition_model_version_stage(Production)
       │
       ▼
[modelo en Production, archive_existing_versions = true automatico]
```

## 7.2 Gates por nivel

| Stage | Gate | Quien |
|---|---|---|
| `None` -> `Staging` | Visual review del dashboard `/reports/<variety>/` (residuos, feature importance, comparacion XGB vs LGB) | Data scientist |
| `Staging` -> `Production` | Quality gate (MAPE < umbral) + A/B vs Production actual + approval | Workflow `promote.yml` + revisor humano |
| `Production` -> `Archived` | Auto al promover una nueva version (archive_existing_versions = true) | MLflow |

## 7.3 Por que el approval humano (GitHub Environment)

Aun con gates automaticos:
- MAPE puede ser engañoso si la distribucion de los predichos cambio
  (modelo "barato" que predice todo igual gana en MAPE pero falla en
  recall extremo).
- A/B en metric absoluta no captura compliance / domain expert
  judgement.
- El approval crea audit log en GitHub (quien aprobo + cuando).

## 7.4 Rollback de un Production

Si la version promovida tiene problemas:

```bash
# Listar versiones del modelo
mlflow search registered-models --filter "name='ml-training-POP'"

# Transition la version vieja de vuelta a Production
mlflow models transition-stage 
    --model-name "ml-training-POP" 
    --version 3 
    --stage Production 
    --archive-existing
```

O via UI: MLflow → Models → `ml-training-POP` → seleccionar version
buena → "Transition to" → Production.

---

> **Cierre Partes 5-7 (patch trainer + CI/CD + promotion).**
>
> Estado actual: el sistema esta production-grade funcional. Tenes:
> - Trainer parchado emitiendo MAPE por variedad a CloudWatch.
> - 4 workflows GitHub Actions: `ci.yml` (lint+build+push), `train.yml`
>   (entrenar desde UI), `promote.yml` (Staging->Production con gate
>   + approval), `terraform-plan.yml` (PR validation).
> - Promotion ciclo completo documentado.
>
> Lo que falta:
> - **Parte 8**: runbook extendido (manuales, recovery).
> - **Parte 9**: costos detallados + modos de operacion.
> - **Parte 10**: hardening (futuro) (TLS, WAF, Multi-AZ, KMS-CMK, VPC endpoints, DR).
> - **Parte 11**: troubleshooting catalogo.
> - **Parte 12**: apendices (glosario, conceptos, diferencias V1->V2).
>
# Parte 8 — Runbook operativo extendido

> **Por que esta parte**: cuando el sistema esta en produccion y vos
> NO estas (vacaciones, fin de semana, te enfermaste), alguien tiene
> que poder operarlo sin leerse las 5 oleadas. Esta parte es ese
> manual: comandos copy-paste con el "por que" al lado para que el
> operador entienda que esta haciendo, no solo ejecute a ciegas.

## 8.1 Manual diario / mas frecuente

### 8.1.1 Re-entrenar una variedad

**Por que se hace**: data nueva subida al bucket (`aws s3 cp` del Excel
acumulado), o pediste un re-train porque cambiaste hiperparametros.

```bash
# Opcion A — via Task (preferido para humanos, polling + exit-code visible)
task batch:train VARIETIES=POP TUNING=prod

# Opcion B — via GitHub Actions UI (preferido si lo dispara alguien sin AWS CLI)
# Actions -> Train -> Run workflow -> POP / prod / wait=true

# Opcion C — via AWS CLI directo (preferido en scripts ad-hoc)
aws lambda invoke 
    --function-name ml-training-dispatcher 
    --cli-binary-format raw-in-base64-out 
    --payload '{"varieties":"POP","tuning":"prod"}' 
    /tmp/out.json
```

**Que pasa por debajo**: Lambda valida el payload (variedad esta en
allowlist, tuning es uno de los 4 validos) → boto3 `submit_job` con
queue Spot (o On-Demand si tuning=prod_xl) → Batch wakea un EC2 c6i.2xlarge
→ pull image → entrenamiento corre → champion log a MLflow → sync a S3
→ container muere → EC2 termina. Todo dura 30-60 min en prod.

### 8.1.2 Re-entrenar TODAS las variedades en un dia (recovery)

**Por que se hace**: rollback de data, hubo un bug en `cli.py` que
hizo que los runs del ultimo mes no se loggearan, o necesitas refresh
total.

```bash
# Loop: una variedad a la vez, espera completion antes de la siguiente
varieties=(POP JUPITER VENTURA SEKOYA ALLISON STELLA)
for v in "${varieties[@]}"; do
    echo "==> Retrain $v"
    task batch:train VARIETIES=$v TUNING=prod || {
        echo "WARN: $v fallo, continuando con el resto"
    }
done
```

**Por que secuencial y no paralelo**: las 6 en paralelo serian
6 × c6i.2xlarge ≈ 48 vCPUs simultaneos. Con `spot_max_vcpus=16`
default, Batch encolaria igual pero te llevarias el cap. Si queres
paralelo de verdad, subir `spot_max_vcpus=48` ANTES (via terraform.tfvars).

### 8.1.3 Spot vs On-Demand por preset (cuando elegir cual)

| Preset | Tiempo estimado | Probabilidad de Spot interrupt | Recomendacion |
|---|---|---|---|
| `smoke`  | 2-4 min  | <1% | Spot SIEMPRE |
| `dev`    | 10-20 min | <2% | Spot SIEMPRE |
| `prod`   | 30-60 min | ~5% | Spot (retry=2 cubre el caso) |
| `prod_xl`| 4-6 h    | 15-30% | **On-Demand** (forzado por dispatcher: tuning=prod_xl → queue ondemand) |

**Por que la regla**: la probabilidad de interrupcion crece con el
tiempo en Spot. Para jobs de 6h, el retry-cost (perder 5h de
computo) supera al 70% de ahorro. La logica esta en `dispatcher.py`:

```python
queue = JOB_QUEUE_ONDEMAND if tuning == "prod_xl" else JOB_QUEUE_SPOT
```

### 8.1.4 Rollback de imagen del trainer

**Por que se hace**: pushaste una version que tiene un bug y queres
volver a la anterior sin re-build.

```bash
# Listar tags del trainer en ECR
aws ecr list-images --repository-name ml-training \
    --query 'imageIds[?imageTag != null].[imageTag]' --output table

# Re-tag la version anterior como :latest
export PREV_SHA="sha-abcdef123456"   # buscar la version anterior buena
export REGION="$AWS_DEFAULT_REGION"
export ACCOUNT="$ACCOUNT_ID"
REG="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

# Pull la imagen vieja
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REG"
docker pull "${REG}/ml-training:${PREV_SHA}"
docker tag  "${REG}/ml-training:${PREV_SHA}" \
            "${REG}/ml-training:latest"
docker push "${REG}/ml-training:latest"
```

**Por que NO actualizar el job-def via Terraform en vez**: porque
Batch arranca el container con la tag puntual configurada en el job-def.
Si en `terraform.tfvars` decis `trainer_image_tag = "sha-abcdef"` y
aplicas, hace lo mismo pero te deja un audit log en el state remoto —
preferible para rollbacks de produccion (en ese caso usar la Opcion
Terraform de abajo).

```bash
# Opcion Terraform (mas auditable)
task infra:apply TARGET=module.batch EXTRA="-var=trainer_image_tag=sha-abcdef123456"
```

## 8.2 Manual semanal / mensual

### 8.2.1 Bajar todo para ahorrar (tear-down)

**Por que se hace**: fin de mes, vacaciones, pausa del proyecto.
Conocido como "scale to near-zero".

```bash
task aws:teardown
# Confirmar con "TEARDOWN"
```

**Por que el orden importa** (ver Parte 4.7.4):
1. **scheduler primero**: si esta arriba, va a re-encender RDS+Fargate
   en el proximo cron y anular el tear-down.
2. **reports + mlflow despues**: son Fargate consumidores; bajan
   primero para que el ALB no tenga targets unhealthy.
3. **batch**: drena Spot CE a 0.
4. **network**: NAT GW es el ultimo en irse — `$32/mes` ahorro
   inmediato.

### 8.2.2 Volver a levantar (rebuild)

```bash
task aws:rebuild
```

**Por que cambia el ALB DNS**: el `aws_lb.main` se recrea con un nombre
distinto en su DNS. Si tenias bookmarks, actualizalos. Si quisieras
un DNS estable, Parte 10.1 (TLS + Route 53) lo resuelve.

### 8.2.3 Subir data nueva

**Por que se hace**: cada mes (o cada cierto periodo) llega un Excel
nuevo con los datos acumulados.

```bash
export BUCKET="${PROJECT}-data-${ACCOUNT_SUFFIX}"

# Subir nuevo Excel (versiones se guardan automaticamente por
# `aws_s3_bucket_versioning` Enabled en modulo storage)
aws s3 cp data/BD_HISTORICO_ACUMULADO.xlsx "s3://${BUCKET}/BD_HISTORICO_ACUMULADO.xlsx"

# Verificar version mas reciente
aws s3api list-object-versions --bucket "$BUCKET" --prefix BD_HISTORICO_ACUMULADO.xlsx \
    --query 'Versions[0].[VersionId,LastModified,Size]' --output table

# Lanzar re-train de todas las variedades con la data nueva
for v in POP JUPITER VENTURA SEKOYA ALLISON STELLA; do
    task batch:train VARIETIES=$v TUNING=prod WAIT=false
done
```

**Por que `wait=false` aca**: 6 jobs encolados, no querras esperar
cada uno secuencial. Los jobs corren en paralelo segun
`spot_max_vcpus`. El monitoreo es via SNS (notifier publica si alguno
FAILED) o `aws batch list-jobs`.

## 8.3 Manual de incidentes

### 8.3.1 Job se quedo en RUNNABLE eternamente

**Sintoma**: `aws batch describe-jobs --jobs <id>` muestra `status =
RUNNABLE` por mas de 10 min sin pasar a `STARTING`.

**Por que pasa**: no hay capacity Spot en `us-east-1a` para
`c6i.2xlarge`, o tu quota de vCPUs esta llena.

**Que mirar**:

```bash
# 1) Estado del CE
aws batch describe-compute-environments 
    --compute-environments ml-training-ce-spot 
    --query 'computeEnvironments[0].status' --output text
# Esperado: VALID. Si dice INVALID, ver statusReason.

# 2) Quota EC2
aws service-quotas get-service-quota 
    --service-code ec2 
    --quota-code L-1216C47A 
    --query 'Quota.Value'

# 3) Estado del Spot fleet implicito (via instancias)
aws ec2 describe-spot-instance-requests 
    --filters Name=state,Values=open,active 
    --query 'SpotInstanceRequests[].[InstanceType,State,Status.Code]' --output table
```

**Fix**:
- Si quota llena: pedir aumento (Capítulo 3.4).
- Si no hay capacity Spot: cancelar el job y resubmit con
  `tuning=prod_xl` (lo manda a la queue On-Demand). O esperar.

### 8.3.2 MLflow 403 "Invalid Host header"

**Por que pasa**: MLflow 3.x rechaza requests cuyo `Host:` header no
esta en `--allowed-hosts`. El V2 ya pone `--allowed-hosts '*'` (3.5.2)
pero si lo cambiaste a una lista, y el ALB DNS no esta — boom.

**Fix**: editar `modules/mlflow/main.tf:container_definitions.command`
para incluir el nuevo DNS, `terraform apply -target=module.mlflow`.

### 8.3.3 RDS "too many connections"

**Por que pasa**: `db.t4g.micro` tiene maximo ~85 conexiones. Si
varias variedades corren en paralelo en Batch y cada una abre 5
conexiones (worker pool), llegas rapido.

**Fix temporal**: cancelar jobs activos en Batch hasta que cuente
baje. **Fix permanente**: subir `rds_instance_class` a `db.t4g.small`
en `terraform.tfvars` (+$13/mes operando).

### 8.3.4 Spot interrupt mid-training

**Sintoma**: job en FAILED con `statusReason = "Host EC2 ..."`.
**Por que pasa**: AWS necesito tu c6i.2xlarge para otro customer.
**Que pasa automaticamente**: `retry_strategy.attempts = 2` + filtro
`Host EC2*` (Parte 3.7.3). El job se re-encola en otra instancia.
**Que hace falta a mano**: nada si pasa una vez. Si pasa
sistematicamente (3+ jobs FAILED por Spot en un dia), considerar:

- Cambiar a `tuning=prod_xl` para esa variedad (queue OD).
- Ver `Best practices > Capacity` en consola Batch — quizas la AZ tiene
  presion. Cambiar `instance_type` a alternativa (`c6a.2xlarge`, `m6i.2xlarge`).

### 8.3.5 `task infra:apply` falla con state lock

**Sintoma**: `Error acquiring the state lock` con un `LockID`.

**Por que pasa**: otra invocacion de `terraform apply` esta corriendo
(o crasheo sin liberar lock).

**Fix**:

```bash
# Ver detalle
aws dynamodb get-item --table-name ml-training-tflock 
    --key '{"LockID":{"S":"<el-ID-del-error>"}}'

# Si el ID corresponde a un proceso que ya murio (laptop crasheada),
# forzar unlock:
task infra:force-unlock LOCK_ID=<LOCK_ID>
```

### 8.3.6 S3 sync del trainer falla con 403

**Por que pasa**: el job-role no tiene `s3:PutObject` sobre
`artifacts_bucket`, o el bucket esta en otra region.

**Que mirar**:

```bash
# Inline policy del job role
aws iam list-role-policies --role-name ml-training-job-role
aws iam get-role-policy --role-name ml-training-job-role --policy-name <name>
```

**Fix**: en Parte 3.7.2 el inline policy `job_s3` ya cubre PutObject
sobre el bucket. Si falla, verificar que el bucket creado matchea
`var.artifacts_bucket`.

### 8.3.7 Job arranca pero MLflow esta apagado (fuera de ventana)

**Por que pasa**: lanzaste un train fuera de L-V 08-12 PET. El cron
de stop apago MLflow (Fargate desired_count=0). El trainer intenta
conectar a `tracking_uri` y obtiene timeout.

**Fix manual** (sin parche 13.2):

```bash
task aws:wake
# Esperar 5-8 min hasta que ALB responde 200
task batch:train VARIETIES=POP
```

> **Solucion permanente**: aplicar §13.2 (auto-train on push con
> wake + cool-down). Ese workflow invoca Lambda scheduler antes del
> train y apaga 10 min despues si MLflow estaba abajo. La ampliacion
> de permisos del role `gha-train` para invocar el scheduler vive
> en §13.2.1.

### 8.3.8 Cold-start de RDS lento el primer request

**Por que pasa**: RDS post-`start_db_instance` tarda ~5 min en estar
disponible. El primer query desde MLflow puede demorar 10-20s extra
por warm-up de buffers.

**Que NO hacer**: no agregues timeout corto en el container — vas a
matar conexiones legitimas. **Que hacer**: el healthcheck del task
def tiene `startPeriod = 60`; aumentalo si tu RDS warmup es
consistentemente mas lento.

## 8.4 Shutdown limpio DENTRO del job de training

**Por que importa**: si Batch te interrumpe (Spot) o vos cancelas el
job, el contenedor recibe `SIGTERM`. El trainer tiene 30s para
limpiar antes de `SIGKILL`. Si en ese momento estabas en medio de
`mlflow.log_model`, el modelo queda corrupto o el run en estado
`RUNNING` para siempre.

El Dockerfile ya tiene `tini` y `STOPSIGNAL SIGTERM` (3.0.5 contracts).
`tini` reenvia el SIGTERM al child Python.

**Que falta en el codigo Python**: un handler de SIGTERM. Patch
opcional:

```python
# main.py — al inicio de main()
import signal

def _graceful_exit(signum, _frame):
    import logging
    logging.getLogger().warning("SIGTERM recibido — abortando run en limpio")
    try:
        import mlflow
        if mlflow.active_run():
            mlflow.set_tag("mlflow.runStatus", "KILLED")
            mlflow.end_run(status="KILLED")
    except Exception:
        pass
    import sys
    sys.exit(143)   # 128 + 15 (SIGTERM)

signal.signal(signal.SIGTERM, _graceful_exit)
```

**Por que NO matar el archivo con `pkill` o `kill -9`**: SIGKILL no
es interceptable. Cualquier estado a medio escribir queda corrupto.

## 8.5 TEAR-DOWN — apagar todo preservando state + datos

Cuando lo uso: vacaciones de 1+ semanas, fin del mes y queres bajar el
gasto, evento de costo inesperado, pausar el proyecto.

**Que SE PRESERVA** (no se borra):

- S3 `ml-training-tfstate-XXXXXX` (Terraform state)
- S3 `ml-training-data-XXXXXX` (Excels de input)
- S3 `ml-training-artifacts-XXXXXX` (modelos serializados + reportes)
- ECR `ml-training`, `ml-training-mlflow`, `ml-training-reports` (todas las tags)
- DynamoDB `ml-training-tflock` (vacia, free tier)
- IAM roles (gha-deploy, batch-execution, lambda-exec, ...)
- OIDC provider de GitHub
- SNS topic + suscripcion email
- EventBridge rules (vacias mientras esten apagadas)

**Que SE APAGA / BORRA temporalmente**:

- ECS Fargate services (MLflow + Reports): `desired_count = 0`
- RDS instance: **stopped** (snapshot automatico antes; se reactiva al
  arrancar)
- Batch compute environments: `desired_vcpus = 0` (no hay EC2 corriendo)
- ALB + listener: borrados (se recrean en rebuild — el DNS cambia)
- NAT Gateway: borrado ($32/mes ahorro)
- Subnets/VPC: se preservan o borran segun el flag (default: preservar)

**Costo despues del tear-down**: ~$8/mes (solo S3 + tflock).

### Comando `task aws:teardown`

```bash
task aws:teardown
# Pide confirmacion: escribir "TEARDOWN" para proceder.
# Pasos internos:
#  1. Verificar que no hay Batch jobs RUNNING/RUNNABLE -> si hay, abort
#  2. ECS update-service desired-count=0 (mlflow + reports)
#  3. Esperar drain de ALB target groups
#  4. terraform apply -target=module.batch -var=spot_max_vcpus=0 -var=ondemand_max_vcpus=0
#  5. RDS stop-db-instance ml-training-mlflow
#  6. terraform destroy -target=module.scheduler   # apaga crons que pueden re-encender
#  7. terraform destroy -target=module.reports
#  8. terraform destroy -target=module.mlflow      # borra ALB + Fargate, mantiene RDS apagado
#  9. terraform destroy -target=module.network     # borra NAT + VPC
# 10. Output del state final (tflock vacio, RDS stopped)
```

### Periodo de gracia de RDS

RDS auto-arranca despues de **7 dias** de estar stopped (limitacion AWS).
Si vas a estar fuera mas de 7 dias, dos opciones:

- **Opcion A (recomendada)**: el scheduler Lambda `ml-training-rds-keepstop`
  (Parte 3.11) detecta que RDS arranco solo y lo vuelve a parar
  automaticamente. Cron: cada 6h chequea state, si RUNNING y fuera de
  ventana lo para.
- **Opcion B**: snapshot manual + delete instance. Para rebuild, restore
  from snapshot (~10 min). Solo si vas a estar fuera 1+ mes.

## 8.6 REBUILD — volver despues de tear-down

Cuando lo uso: vuelvo de vacaciones, retomo el proyecto, necesito la UI
de MLflow para mirar runs viejos.

**Precondicion**: la cuenta tiene los recursos preservados del tear-down
(buckets S3, ECR, tflock, IAM, OIDC).

### Comando `task aws:rebuild`

```bash
task aws:rebuild
# Pasos internos:
#  1. terraform init -reconfigure   # state remoto en S3 + lock DDB
#  2. RDS start-db-instance ml-training-mlflow  (~5 min cold start)
#  3. terraform apply -target=module.network    # VPC + NAT + SGs
#  4. terraform apply -target=module.mlflow     # Fargate + ALB
#  5. terraform apply -target=module.reports    # Fargate reports
#  6. terraform apply -target=module.batch -var=spot_max_vcpus=16  # restore queues
#  7. terraform apply -target=module.scheduler  # restore crons
#  8. Smoke check: curl http://<new-ALB-DNS>/  -> 200 OK
#  9. Output: el ALB DNS nuevo (cambia respecto al stand-up original)
```

**Tiempo**: 20-30 min, dominado por RDS cold start (5 min) + Fargate
task launch (~3 min) + ALB target registration (~2 min).

**Lo unico que cambia respecto al stand-up original**: el DNS del ALB.
Si tenías bookmark, actualizalo. (Si en Parte 10.1 agregaste un dominio
custom via Route53, el dominio sigue igual; sólo el record A apunta al
nuevo ALB.)

## 8.7 DESTROY — eliminar TODO de la cuenta AWS

Cuando lo uso: cierre del proyecto, migracion a otra cuenta, hard reset
para empezar de cero.

> **Nota importante**: esta seccion solo aplica si ya estuviste operando
> el sistema por un tiempo y queres salir. En el stand-up inicial (Parte
> 1.1) no hay nada que respaldar — los backups de abajo presuponen que
> tenes modelos registrados, una RDS poblada y un Terraform state con
> recursos. Si no tenes nada de eso, salta directo al comando.

**ATENCION**: esto borra:

- TODOS los buckets S3 (incluido tfstate — perdes el historial de cambios
  de Terraform; los modelos en `s3://artifacts/`; los Excels en `s3://data/`)
- TODOS los repos ECR (perdes todas las tags / versiones de imagenes)
- RDS instance + snapshots automaticos (perdes el Model Registry entero —
  todas las versiones, transitions, tags)
- IAM roles + OIDC provider (proximo deploy desde GHA va a fallar hasta
  recrear)
- VPC + NAT + ALB + Fargate + Batch + Lambdas + EventBridge + SNS +
  CloudWatch alarms + log groups

**ANTES de destruir, hacer 3 backups manuales**:

```bash
# Pre-requisito: tener un bucket de archive FUERA del proyecto (otra cuenta
# o, como minimo, otro nombre que NO sea destruido por este `aws:destroy`).
# Crearlo a mano si no existe (este bucket vive aparte del state):
export ARCHIVE_BUCKET="${PROJECT}-archive-${ACCOUNT_SUFFIX}"
if ! aws s3api head-bucket --bucket "${ARCHIVE_BUCKET}" 2>/dev/null; then
  aws s3api create-bucket --bucket "${ARCHIVE_BUCKET}" --region "${AWS_DEFAULT_REGION}"
  aws s3api put-bucket-versioning --bucket "${ARCHIVE_BUCKET}" \
    --versioning-configuration Status=Enabled
fi

# (1) Export del Model Registry a JSON (corre con MLflow encendido)
export MLFLOW_TRACKING_URI="http://<ALB-DNS>/"
mlflow models search > artifacts/model-registry-export.json
aws s3 cp artifacts/model-registry-export.json \
  "s3://${ARCHIVE_BUCKET}/ml-training-$(date +%Y-%m-%d)/"

# (2) Snapshot manual de RDS (queda independiente del instance)
aws rds create-db-snapshot \
  --db-instance-identifier ml-training-mlflow \
  --db-snapshot-identifier "ml-training-mlflow-final-$(date +%Y-%m-%d)"

# (3) Export del Terraform state como ultimo backup
cd infra/envs/prod
terraform state pull > /tmp/tfstate-final-backup.json
aws s3 cp /tmp/tfstate-final-backup.json \
  "s3://${ARCHIVE_BUCKET}/ml-training-$(date +%Y-%m-%d)/"
cd ../../..
```

> **Por que un bucket separado**: `aws:destroy` borra todos los buckets
> del proyecto (`ml-training-data-*`, `ml-training-artifacts-*`,
> `ml-training-tfstate-*`). Si pusieras los backups ahi, se borrarian
> en el mismo apply. El `${PROJECT}-archive-${ACCOUNT_SUFFIX}` queda
> intacto porque Terraform no lo conoce.

### Comando `task aws:destroy`

```bash
task aws:destroy
# Pide confirmacion DOBLE: escribir "DESTROY <PROJECT>" para proceder.
# Pasos internos:
#  1. Pre-flight: chequear que ningun bucket tiene objetos sin versioning OFF
#  2. terraform destroy en orden inverso de dependencias
#  3. Para cada bucket S3 con versioning ON: empty (incluye versions + delete markers)
#  4. terraform destroy de modules.storage (borra los buckets ya vacios)
#  5. Borrar bucket tfstate manualmente (Terraform no puede destruir su propio backend)
#  6. Borrar tabla DynamoDB tflock
#  7. Borrar OIDC provider (si no esta compartido con otros repos)
#  8. Borrar SLR si existe (raro, usualmente AWS lo mantiene)
```

**Tiempo**: 30-45 min, dominado por el vaciado de buckets versionados
(cada modelo es ~10 MB con N versions).

---

# Parte 9 — Costos detallados

> **Por que esta parte**: AWS no te muestra un total previsto antes
> de gastar. Si no entendes el desglose, te llevas sorpresa en la
> factura. Esta parte te da el numero realista por modo de operacion
> y los `dials` para bajarlo.

## 9.1 Escenario lockeado: scheduler L-V 08-12 PET — ~$68/mes

Asume: 80 horas/mes de MLflow encendido (4h × 5 dias × ~4 semanas),
10 trainings/mes promedio (1h cada uno), 5 GB de S3 storage total,
3 GB de ECR images.

| Item | Calculo | Mensual (USD) |
|---|---|---|
| S3 (5 GB Standard) | 5 × $0.023 | $0.12 |
| S3 (versiones no-current con lifecycle 90d) | ~10 GB | $0.23 |
| ECR (3 GB) | 3 × $0.10 | $0.30 |
| DynamoDB tflock (on-demand) | <1k requests | $0.01 |
| RDS db.t4g.micro (80h/mes) | 80 × $0.018 | $1.44 |
| RDS storage (20 GB gp3) | 20 × $0.115 | $2.30 |
| Fargate MLflow (2 vCPU, 4 GB, 80h) | 80 × ($0.04048 × 2 + $0.004445 × 4) | $7.90 |
| Fargate Reports (0.5 vCPU, 1 GB, 80h) | 80 × ($0.04048 × 0.5 + $0.004445 × 1) | $1.97 |
| ALB (24/7) | 720h × $0.0225 | $16.20 |
| ALB LCU | <0.5 LCU promedio | $0.50 |
| NAT Gateway (24/7) | 720h × $0.045 | $32.40 |
| NAT egress data | 10 GB | $0.45 |
| EC2 Spot c6i.2xlarge (10 jobs × 1h) | 10 × $0.102 | $1.02 |
| Lambdas (negligible) | ~1000 invocs/mes | $0.10 |
| EventBridge | ~120 events/mes | $0.10 |
| SNS | ~10 publishes/mes | $0.01 |
| CloudWatch Logs (14d retention, 1 GB) | 1 × $0.50 | $0.50 |
| CloudWatch Custom Metrics (N MAPE + 3 base; N=6 ejemplo) | (N+3) × $0.30 | $2.70 |
| CloudWatch PutMetricData API | ~10k calls/mes | $0.01 |
| Data transfer (ALB out a Internet) | 5 GB | $0.45 |
| **Total** | | **~$68** |

> **Notas del calculo**:
> - **Custom Metrics $0.30/serie**: cobro por metrica unica (combinacion
>   namespace + name + dimensiones). MAPE con `dim=variety` genera **N
>   series** (una por variedad en `var.varieties`) + 3 metricas base de
>   Batch/ALB = **N+3 series**. El ejemplo usa N=6 (default actual) → 9
>   series × $0.30 = $2.70. Si cambias `varieties`, este item escala
>   lineal: cada variedad nueva = +$0.30/mes. Si tu trafico de
>   PutMetricData superara 1M calls/mes, sumar $0.01/1000 calls
>   (despreciable aca).
> - **Data transfer ALB out 5 GB**: trafico de la UI MLflow + reports
>   hacia tu browser desde Internet. NO se cuenta el egress AWS→AWS
>   (Batch→S3, ECS→RDS) porque va por la VPC interna sin costo. El
>   item "NAT egress 10 GB" de arriba ya cubre lo que SALE de la VPC.

**Por que el numero no es exactamente $64**: la V1 daba $64 con
asunciones distintas (sin Reports Fargate, NAT egress no contado).
$68 es realista con los modulos del V2.

## 9.2 Comparativa con escenarios alternativos

| Escenario | Cambio vs default | Costo total/mes | Cuando elegirlo |
|---|---|---|---|
| **Hibernado** | tear-down completo (§8.5) | ~$3 | Pausa de 1+ semana |
| **Default (lockeado)** | Scheduler L-V 08-12 PET | **~$68** | Operacion normal |
| **24/7** | Scheduler OFF, MLflow + RDS siempre on | ~$140 | Equipo distribuido multi-timezone |
| **No-NAT** | VPC endpoints en vez de NAT GW (Parte 10.3) | ~$36 | Trafico NAT < 10 GB/mes |
| **Multi-AZ RDS** | RDS Multi-AZ (Parte 10.4) | +$13 sobre default | Compliance / SLA estricto |
| **TLS + Custom Domain** | Route 53 zone + ACM cert (Parte 10.1) | +$1 sobre default | Exposicion publica |

## 9.3 Matriz de costos por modo de lifecycle

Tabla resumen que cruza los 4 modos (STAND-UP / TEAR-DOWN / DESTROY) con
los recursos: util para razonar "cuanto bajo apagando X" antes de
ejecutar `task aws:teardown` o `task aws:destroy` (los modos en si estan
documentados en §8.5-§8.7).

| Recurso | STAND-UP (operando) | TEAR-DOWN (hibernado) | DESTROY (vacio) |
|---|---|---|---|
| S3 (todos los buckets, ~5 GB) | $0.12 | $0.12 | $0 |
| ECR (3 repos, ~3 GB) | $0.30 | $0.30 | $0 |
| DynamoDB tflock (on-demand) | $0.01 | $0.01 | $0 |
| RDS db.t4g.micro (L-V 08-12 PET ≈ 80h/mes) | $1.50 | $0 (stopped) | $0 |
| RDS allocated storage (20 GB gp3) | $2.30 | $2.30 (storage solo) | $0 |
| ECS Fargate MLflow (2 vCPU, 4 GB, 80h/mes) | $4 | $0 | $0 |
| ECS Fargate Reports (0.5 vCPU, 1 GB, 80h/mes) | $0.50 | $0 | $0 |
| ALB | $16 | $0 (borrado) | $0 |
| NAT Gateway (24/7) | $32 | $0 (borrado) | $0 |
| Batch EC2 (Spot c6i.2xlarge, ~10 jobs/mes × 1h) | $3 | $0 | $0 |
| Lambdas (negligible) | $0.10 | $0.10 | $0 |
| EventBridge / SNS / CloudWatch | $0.30 | $0.30 | $0 |
| Data transfer (NAT egress + ALB) | $5 | $0 | $0 |
| **Total mensual** | **~$68** | **~$3** | **$0** |

> La suma directa de esta tabla da ~$65; los ~$68 reales (que matchean
> §9.1) incluyen items consolidados aca: ALB LCU + CloudWatch Custom
> Metrics (9 series MAPE+base × $0.30) + DynamoDB + S3 lifecycle de
> versiones. Tabla pensada como **delta entre modos**, no como suma
> auditable — para esa ver §9.1.

Si queres bajar mas el modo operando, ver Parte 10.3 (VPC endpoints
elimina NAT GW = $32 menos), §9.4 (S3 lifecycle + Intelligent
Tiering, ECR scan policies).

## 9.4 Optimizaciones adicionales ((futuro))

**Por que se llaman "futuras" en vez de aplicarlas dia 1**: cada una
tiene un costo de ingenieria o un trade-off. Aplicarlas dia 1 te frena
sin que aporten valor hasta que la operacion tenga datos.

### 9.4.1 VPC endpoints en vez de NAT (Parte 10.3)

Cambia $32/mes NAT GW por $7/mes en 4 endpoints (S3, ECR, CloudWatch,
STS). Net: $25/mes menos. **Por que no se aplica dia 1**: agrega
~100 lineas de Terraform y rompe si te falta un endpoint para algun
servicio que el trainer use indirectamente. Aplicarlo despues, cuando
tenes lista de servicios consumidos en CloudTrail.

### 9.4.2 S3 Intelligent-Tiering

Auto-tier `artifacts/` despues de 90d a Standard-IA (-50% storage).
**Por que no dia 1**: tu volumen S3 es ~5 GB. Ahorro real: <$0.5/mes.
No vale la pena hasta que pases los 100 GB.

### 9.4.3 ECR scan policies

Borrar imagenes con vulnerabilidades CVSS > 7. **Por que no dia 1**:
genera ruido (la imagen base puede tener CVEs que upstream parchea
en semanas). Aplicar despues de la primera vuelta.

### 9.4.4 Fargate Spot para MLflow

50-70% off Fargate, pero interrupcion = MLflow caido. **Por que no
dia 1**: MLflow es path-critical para CI/CD; downtime mid-day rompe
tu workflow. Usar solo en envs/dev.

---

# Parte 10 — Hardening production-grade ((futuro))

> **Por que es FUTURO y no dia 1**: los items aca cuestan ingenieria
> (~1-2 dias cada uno) y/o $$. Sin estar en produccion real con
> usuarios, no hay senial de cual aplicar primero. **Re-leer esta
> parte a los 90 dias** de operacion: ahi vas a saber por incidente
> real cual era prioritario.

## 10.1 TLS en el ALB (HTTPS)

**Por que se hace**: HTTP puro permite MITM en cualquier red. Tambien
es prereq para WAF y CloudFront.

**Plan**:

1. Comprar/registrar dominio en Route 53 (~$12/anio).
2. ACM cert para `mlflow.tu-dominio.com` (auto-renew, $0).
3. Agregar listener HTTPS en el ALB con cert ACM.
4. Default action del listener HTTP: redirect 301 a HTTPS.
5. Output del Terraform: `https://mlflow.tu-dominio.com/`.

**Por que no dia 1**: si el ALB es interno-only (VPN/SG restrictivo)
el riesgo MITM es minimo. Aplicar cuando expongas a Internet abierta.

## 10.2 WAF v2 sobre el ALB

**Por que se hace**: protegerte de OWASP top 10 + rate limiting.
Specially util si tu ALB recibe trafico publico.

**Costo**: $5/mes WAF + $1 por rule + $0.60 per million requests.

**Por que no dia 1**: WAF managed rules tienen falsos positivos que
pueden bloquear tu propio workflow (ej. CSP rules sobre MLflow UI).
Necesitas observabilidad antes para tuning.

## 10.3 VPC endpoints (eliminar NAT GW)

**Por que se hace**: ahorra $32/mes (NAT GW horario) + mejora latencia
(trafico AWS-AWS no sale a Internet).

**Endpoints necesarios** (gateway-type S3 + interface-type para los
demas):

- `com.amazonaws.us-east-1.s3` (gateway, gratis)
- `com.amazonaws.us-east-1.ecr.api` (interface, ~$7.20/mes)
- `com.amazonaws.us-east-1.ecr.dkr` (interface, ~$7.20/mes)
- `com.amazonaws.us-east-1.logs` (interface, ~$7.20/mes)
- `com.amazonaws.us-east-1.sts` (interface, ~$7.20/mes)
- `com.amazonaws.us-east-1.secretsmanager` (interface, ~$7.20/mes)

**Cuenta real**:

- Endpoints fijos: 5 interface × $7.20/mes = **$36/mes** (gateway de S3 es gratis).
- NAT GW que se elimina: **-$32/mes** (horario) **-$0.045/GB egress**.
- Total horario: **+$4/mes mas caro** que NAT.

**Donde se gana**: data transfer. NAT cobra $0.045/GB egress; via
endpoints es $0.01/GB (interface) o $0 (S3 gateway). Si tu trafico
mensual supera ~80 GB el ahorro empieza a comer el +$4 fijo y a
partir de ~150 GB ya es ganancia neta.

Para este proyecto (smoke ~100 MB, prod ~1 GB por job, ~30 jobs/mes ≈
30 GB) el net es NEGATIVO: NAT sigue siendo mas barato. Por eso esta
optimizacion es "(futuro)" — aplicar cuando el trafico crezca o si
te molesta el blast radius del NAT (e.g. compliance).

## 10.4 Multi-AZ RDS

**Por que se hace**: RDS single-AZ tiene SLA 99.5%. Multi-AZ pasa a
99.95% (replica sincrona en otra AZ + auto-failover).

**Costo**: 2× el precio de la instancia (replica activa). De $13/mes
operando pasa a $26/mes.

**Por que no dia 1**: MLflow no es path-critical para el trainer (el
training corre, registra y el MLflow puede estar caido un par de horas
sin afectar). Aplicar si MLflow se vuelve critical-path
(ej. promotion automatica via API en CI).

## 10.5 KMS-CMK en vez de AES256

**Por que se hace**: KMS-CMK te da control sobre las keys, audit log
detallado por KMS API call, rotacion automatica, cross-account access.

**Costo**: $1/mes por CMK × ~5 CMKs = $5/mes.

**Por que no dia 1**: AES256 (SSE-S3 default) ya es AES con keys
manejadas por AWS. La diferencia es _quien_ tiene la key, no _que tan
fuerte_ es el encrypt. Aplicar cuando compliance lo exija.

## 10.6 DR cross-region

**Por que se hace**: si `us-east-1` se cae entera (raro pero pasa, ej.
2017, 2021), no perdes acceso a modelos.

**Plan**: replicar `artifacts/` bucket a `us-west-2`, snapshot RDS
diario cross-region. Costo: $0.02/GB transfer + $0.023/GB storage en
us-west-2.

**Por que no dia 1**: para un proyecto de 1 cuenta de 1 dev, el coste
operativo de DR (testing del failover, mantener Terraform en 2
regiones) supera el ROI del eventual outage.

## 10.7 ECR image signing (cosign + AWS Signer)

**Por que se hace**: garantizar que la imagen que corre en Batch es
la misma que CI pusheo, sin alterar.

**Costo**: $0.50/mes por signing profile.

**Por que no dia 1**: si solo vos pusheas a ECR y la account tiene
buen IAM, el riesgo de tampering es bajo. Aplicar en regulated
industries (financial, healthcare).

## 10.8 Decisiones a revisar a los 90 dias

| Decision | Revisar si... | Accion |
|---|---|---|
| TLS off (10.1) | expones a Internet | Activar |
| WAF off (10.2) | tenes trafico publico | Activar despues de observabilidad |
| NAT GW (10.3) | trafico NAT > 10 GB/mes O policy zero-egress | Migrar a endpoints |
| RDS single-AZ (10.4) | MLflow es path-critical en CI | Multi-AZ |
| AES256 (10.5) | compliance (HIPAA, PCI, SOC2) | KMS-CMK |
| Single-region (10.6) | uptime > 99.9% es contractual | DR cross-region |
| ECR sin signing (10.7) | regulated industry | cosign + Signer |

---

# Parte 11 — Troubleshooting (catalogo)

> **Por que esta parte**: errores que YA pasaron (en V1 o durante el
> desarrollo del V2). Cada uno con sintoma, causa, y fix concreto.
> Si tu error no esta aca: `aws logs tail /aws/batch/ml-training --follow`
> es siempre el primer paso.

| # | Sintoma | Causa | Fix |
|---|---|---|---|
| 1 | `terraform init` falla con `failed to retrieve credentials` | `AWS_PROFILE` no exportado o profile inexistente | `aws sts get-caller-identity` para verificar; `$AWS_PROFILE = "..."`. |
| 2 | `terraform apply` cuelga en `aws_db_instance.mlflow: Still creating...` por 15+ min | RDS create normal toma 8-12 min; si > 15 min hay un problema | Re-intentar; chequear subnet group AZs distintas; ver eventos en AWS console RDS |
| 3 | `aws_lambda_function: InvalidParameterValueException: The role defined for the function cannot be assumed by Lambda` | El role recien creado tarda en propagarse | `sleep 10 && terraform apply` (race-condition AWS) |
| 4 | Batch job en `SUBMITTED` por 5+ min | El CE no escalo todavia | Normal; espera. Si > 15 min, revisar quotas (8.3.1) |
| 5 | Trainer en log: `Error 28: out of memory` | OOM al cargar el Excel grande con todas las variedades | Bajar `parallel_varieties` a 1 (default), o subir job-def `memory` |
| 6 | Trainer en log: `mlflow.exceptions.MlflowException: API request failed` | MLflow apagado / cold-start | Esperar 5 min; verificar `aws_ecs_service.mlflow` desired=1 |
| 7 | Dashboard `/reports/POP/` da 404 | Sync de S3 a nginx no corrio | Esperar 60s (loop sync); o entrar al container: `aws ecs execute-command ...` |
| 8 | GitHub Actions falla con `Could not assume role` | Trust policy del role no incluye el sub `repo:org/repo:*` | Re-aplicar `module.cicd` con `github_org` + `github_repo` correctos |
| 9 | Alarma `ml-training-mape-pop` no dispara aunque MAPE es alto | El trainer no publica a CloudWatch | Verificar Parte 5 patch aplicado + IAM `cloudwatch:PutMetricData` |
| 10 | `terraform destroy` en `module.network` falla con `DependencyViolation` | Algo en otra capa todavia usa la VPC (ALB, ENI huerfana) | `terraform destroy` modulo por modulo en orden inverso |
| 11 | ECR push falla con `denied: Your authorization token has expired` | Token tiene 12h de validez | `aws ecr get-login-password ... | docker login ...` de nuevo |
| 12 | `aws_secretsmanager_secret`: `cannot be deleted before the recovery window` | AWS deja 7 dias minimum para recovery | Usar `aws secretsmanager delete-secret --force-delete-without-recovery` |
| 13 | RDS arranco solo despues de 7 dias stopped | Hard limit AWS — auto-arranque post-7d | Scheduler keepstop (3.10.2) lo re-para cada 6h |
| 14 | Workflow `train.yml` falla con `Unable to locate credentials` | Permissions `id-token: write` faltante en YAML | Agregar `permissions: { id-token: write, contents: read }` al job |
| 15 | Lambda dispatcher 500: `variedades no permitidas: ['xyz']` | Variety no esta en `varieties_allowed` del terraform | Agregar a `var.varieties_allowed` y `terraform apply -target=module.lambdas` |
| 16 | NAT GW cuesta mas de lo esperado | Trafico NAT alto (mucho egress S3 cross-region o ECR pulls grandes) | Activar VPC endpoints (10.3) |

---

# Parte 12 — Apendices

## Apendice A — Glosario (referencia rapida)

| Termino | Que es en 1 frase |
|---|---|
| **ALB** | Load Balancer L7 de AWS. Aca expone MLflow + Reports en :80. |
| **AWS Batch** | Servicio que corre jobs ephemera en EC2. Autoescala 0↔N segun cola. |
| **CE (Compute Environment)** | En Batch, define las EC2 disponibles (tipo, Spot/OD, min/max vCPUs). |
| **ECR** | Registry Docker privado de AWS. |
| **ECS Fargate** | Modo serverless de ECS. No manejas EC2. |
| **EventBridge** | Bus de eventos de AWS. Cron, eventos de servicios, custom. |
| **IaC** | Infrastructure as Code (Terraform aca). |
| **MLflow** | Tracking + Registry de modelos ML. |
| **NAT GW** | Gateway para que subnets privadas salgan a Internet. $32/mes. |
| **OIDC** | OpenID Connect. GitHub Actions lo usa para asumir IAM roles sin secrets. |
| **RDS** | Postgres managed de AWS (backend de MLflow). |
| **SLR (Service Linked Role)** | IAM role que AWS crea solo para que un servicio funcione. |
| **Spot** | EC2 70% mas barato pero interrumpible con 2 min de aviso. |
| **State (Terraform)** | JSON con mapping HCL ↔ recursos reales. Vive en S3 + lock DDB. |
| **STS** | Servicio AWS que emite credenciales temporales (asume role). |

## Apendice B — Conceptos fundamentales (lectura opcional)

### B.1 Por que MLOps y no "el script de Python que corre en CRON"

3 problemas que MLOps resuelve:
1. **Reproducibilidad**: tu modelo no se reproduce desde una notebook
   sin Docker + git SHA + MLflow tag.
2. **Auditabilidad**: cuando un cliente reclama una prediccion mala
   de hace 3 meses, sabes que version del modelo predijo y con que
   data.
3. **Observabilidad**: MAPE silencioso es bug silencioso. Alarmas
   automaticas convierten degradacion en pager.

### B.2 Por que Terraform y no CDK / Pulumi

- **Terraform**: declarativo, HCL, ecosistema gigante, multi-cloud.
  Estandar de industria.
- **CDK**: TypeScript/Python imperativo que GENERA CloudFormation.
  AWS-only.
- **Pulumi**: TypeScript/Python imperativo nativo multi-cloud.

Terraform gana en V2 porque:
- Tu equipo es mas probable que conozca HCL que CDK.
- El state remoto en S3 + DDB es 50 lineas, no CDK Pipelines de 500.
- La modularidad HCL es mas explicita que CDK constructs.

Si en el futuro queres Pulumi (mejor IDE, types), la migracion del
state es soportada (`pulumi import`).

### B.3 Por que Task si Terraform ya orquesta

Terraform es declarativo: te dice _que_ infra existe, no _en que orden_
hacer cosas que dependen entre si. Ejemplos donde Task gana:

- "Antes de bajar Fargate, drena Batch" — orden + condicion (pre-check
  con exit 1 si hay jobs RUNNING, en `cluster:scale-down`).
- "Push ECR, despues `terraform apply -target=module.batch`" — multi-tool
  encadenado (`aws:deploy` hace ambos en oleadas A+B+C).
- "Si RDS esta stopped, start; espera available; despues apply" — flujo
  con polling (`cluster:wait-healthy` chequea ALB hasta 200 con timeout).

**Vs Ansible (V1 deprecated)**: ambos resuelven lo mismo, pero Task es
single-binary 10 MB vs Ansible ~200 MB con Python+pipx, sintaxis
YAML+POSIX shell mas legible que YAML+Jinja+modulos `ansible.builtin.X`,
y corre con un solo binario en Linux/WSL/macOS sin overhead de runtime
Python. (En este proyecto Task se instala dentro de WSL Ubuntu — ver
Capítulo 3.1 — para mantener un unico entorno bash a lo largo de toda la
guia.) Para un stack Docker + AWS managed services (sin
hosts EC2), Ansible es overkill.

**Vs bash/Makefile**: Task da una sola lista descubrible (`task --list`),
namespacing con `includes:`, cache de builds via `sources:` con hash
(no solo timestamps como Make), `prompt:` para destructivos, variables
con scope, y manejo de errores estructurado por task. Bash sirve para
1-2 scripts; Task para 30+ tasks organizadas por dominio.

### B.4 Por que GitHub Actions con OIDC

OIDC remplaza el caso comun de access keys de larga duracion:

- AWS access keys leakean (PRs maliciosos, dumps de logs accidentales).
- GitHub PAT leakean en clonados.
- OIDC = credenciales de 60 min firmadas por GitHub para tu sub
  (repo:org/repo:ref). Si tu repo se hackea, el atacante NO obtiene
  AWS keys.

## Apendice C — Diferencias V1 → V2 (changelog)

### Cambios estructurales

| V1 (6537 lineas) | V2 (~5000 lineas) | Por que |
|---|---|---|
| Sec 0 a 14 lineales | Partes 0-12 lineales | Mismo orden, nombres mas claros |
| P.2 Glosario + P.3 Conceptos al frente | Apendice A + B al final | El audience experto NO los necesita |
| Sec 8.5 "Bajar todo" mezclado con runbook | Parte 1 (Lifecycle) propia | 4 modos distintos merecen estructura propia |
| Sec 10 "Hardening" sin priorizacion | Parte 10 con "revisar a 90d" | Decision support, no laundry list |

### Bugs / inconsistencias corregidas

| V1 | V2 |
|---|---|
| Alarma `mape_pop` hardcoded a POP | Alarmas dinamicas via `for_each = toset(var.varieties)` en monitoring (3.8.2) |
| Codigo Lambda no incluido en repo | `infra/lambdas/dispatcher.py`, `notifier.py`, `scheduler.py` con codigo completo y tested |
| `timeout-minutes: 420` rigido en train.yml | `timeout-minutes: 480` matchea `job_attempt_seconds = 28800` del job-def |
| Modulo `cicd/` en `envs/cicd/` separado | Modulo `cicd/` en `envs/prod/main.tf` (un apply, no bootstrap aparte) |
| Bootstrap solo en bash | Bootstrap en bash (`bootstrap.sh`) — corre en WSL en Windows |
| Variedades sin allowlist en Lambda | Allowlist enforcement: validation falla early con 400 si variety no esta |
| Random Forest mencionado | XGBoost + LightGBM (matchea `src/step_04_train/registry.py`) |
| Target sin nombrar | Target `KG/JR_H` (kg cosechados / jornal-hora) documentado |

### Sintaxis / convenciones

| V1 | V2 |
|---|---|
| Mix de bash y PowerShell | **bash everywhere** desde WSL Ubuntu (memoria `feedback_shell_bash.md`) |
| `infra/bootstrap.sh` | `infra/bootstrap.sh` + `infra/bootstrap-oidc.sh` (separados) |
| Comandos sin variables session | `$PROJECT`, `$ACCOUNT_SUFFIX`, etc. al inicio (0.4) |

### Refactor post-V2 (limpieza Terraform + Task)

Cambios aplicados despues de finalizar V2 para reducir duplicacion y
acoplamiento. Todos preservan comportamiento (cero diff en `terraform plan`,
mismo grafo de tasks). Solo cambia organizacion del codigo.

**Infra / Terraform:**
- NUEVO: `infra/modules/_shared/` con trust policies JSON + .tftpl compartidos.
  Cada modulo carga la trust policy con `file()` o `templatefile()` en vez de
  redeclarar `data "aws_iam_policy_document"` (5 copias eliminadas: 3x
  `ecs_tasks_assume`, 2x `lambda_assume`) o `jsonencode({...})` inline (3x
  para `batch.service`, `consumer.role`, `gha-deploy`).
- NUEVO: modulo `consumer-iam/` (Patch 13.5) — rol OIDC para repo consumer
  read-only sobre S3 artifacts (FastAPI/Streamlit que sirve modelos).
- Lambdas `dispatcher` y `scheduler` ahora reciben `job_queue_spot_name` /
  `job_queue_ondemand_name` como variables (antes reconstruian
  `"${var.project}-job-queue-spot"` inline). Wireado desde `envs/prod/main.tf`
  via `module.batch.job_queue_spot` y `.job_queue_ondemand` (outputs ya
  existentes pero no consumidos).
- `lambdas/notifier.py`: `AWS_REGION` y `BATCH_LOG_GROUP` ahora son
  `os.environ[...]` (hard requirement). Antes tenian defaults engañosos que
  enmascaraban env vars faltantes.

**Tasks / Taskfile:**
- `Taskfile.yml` raiz: nueva var global `REGION`, e `includes:` propaga
  `PROJECT` + `REGION` a cada include via `vars:`. Las redeclaraciones de
  esos defaults en cada task .yml se eliminaron.
- NUEVO: `tasks/lib/batch_wait.sh` con `wait_job()` compartido. `batch:train`
  y `batch:train-lambda` hacen `source tasks/lib/batch_wait.sh` en vez de
  inlinear la funcion bash en cada task.
- `tasks/aws.yml`: `_empty-bucket` unificado con flag `DELETE` (acepta full
  bucket name); `_delete-tfstate-bucket` eliminado (era 95% identico).
- `tasks/cluster.yml`: nuevo helper `_assert-no-running` extraido de
  `scale-down` (pre-check de Batch jobs RUNNING antes de apagar).
- `tasks/ecr.yml:build`: los 3 `case` shell para resolver IMAGE_NAME /
  DOCKERFILE / RESOLVED_TAG por IMG se reemplazaron por templating go-task
  nativo (`{{if eq .IMG "trainer"}}...{{else}}...{{end}}`). Quita 3 fork de
  shell por invocacion.
- `tasks/mlflow_registry.yml`: doc bug fixed — header decia `VARIETY=POP`
  pero las tasks requieren `MODEL_NAME=rnd-forest-POP`. Tambien arreglado en
  `Taskfile.yml` `help:aws` y `default`.

Verificacion: `terraform validate` OK, `task --list` parsea, `task --summary
batch:train` muestra `PROJECT=ml-training` y `REGION=us-east-1` propagandose
correctamente a los includes.

## Apendice D — Mapa de archivos creados por la guia

Al terminar las 5 oleadas, tu repo tiene **agregados** (todo lo que
ya estaba se preserva):

```
ml_training/
├── infra/                              # NUEVO
│   ├── bootstrap.sh                    # Parte 2.2
│   ├── bootstrap-oidc.sh               # Parte 2.5
│   ├── envs/prod/                      # Parte 3.2 (6 archivos)
│   ├── modules/                        # Parte 3 (9 modulos × 2-3 archivos cada uno)
│   │   └── consumer-iam/               # Parte 13.5 (solo si aplicas Parte 13)
│   └── lambdas/                        # Parte 3.9, 3.10 (3 archivos .py)
├── tasks/                              # NUEVO (orquestacion AWS + helpers locales)
│   ├── infra.yml                       # Parte 4.1.4   terraform + bootstrap
│   ├── ecr.yml                         # Parte 4.1.5   build + push 3 imagenes
│   ├── batch.yml                       # Parte 4.1.6   submit jobs + polling (+ train-lambda)
│   ├── cluster.yml                     # Parte 4.1.7   lifecycle scale up/down
│   ├── mlflow_registry.yml             # Parte 4.1.8   promote con gate MAPE + A/B
│   ├── aws.yml                         # Parte 4.1.9   orquestadores high-level (+ nuke)
│   └── local.yml                       # Parte 4.1.11  helpers dev local (ensure-buckets)
├── Taskfile.yml                        # MODIFICADO (Parte 4.1.3 anade includes:)
├── docker/                             # SOLO NUEVO el subdir reports/
│   └── reports/                        # Parte 3.6 (Dockerfile + nginx.conf + entrypoint.sh)
├── .github/workflows/                  # NUEVO
│   ├── deploy.yml                      # Parte 6.2 (consolida ci + terraform-plan + infra-apply)
│   ├── training.yml                    # Parte 6.4 (consolida train + auto-train + promote)
│   └── destroy.yml                     # Parte 6.5.5 (3 modos: TEAR-DOWN / DESTROY / NUKE)
├── src/                                # MODIFICADO
│   ├── orchestration/variety_runner.py # Parte 5.3 (agrega emit_mape_metric tras quality gate)
│   └── utils/cloudwatch_metrics.py     # Parte 5.2 (archivo nuevo)
├── main.py                             # OPCIONAL: signal handler (8.4)
├── GUIA_MLOPS_AWS_V2.md                # esta guia
└── (resto del proyecto)
```

Total agregado: ~70 archivos nuevos, ~2 modificados (~72 con Parte 13
aplicada). Total LOC agregadas: ~3500 (HCL + Python + YAML + Markdown).

**Archivos extra si aplicas Parte 13** (customizaciones):
- `infra/modules/consumer-iam/` (3 archivos: variables.tf, main.tf,
  outputs.tf) — §13.5.
- `.github/workflows/auto-train-on-push.yml` — §13.2.
- Modificaciones a `infra/envs/prod/{main,variables,outputs}.tf` y
  `terraform.tfvars` para registrar el modulo `consumer_iam` — §13.5.2.

---

> **Cierre Tramo II — guía completa.**
>
> Para usar esta guía desde el día 1:
> 1. Validar prereqs en **Capítulo 3**, luego ejecutar **Capítulo 4** (local).
> 2. Si nunca aplicaste nada en AWS: empezar en **Parte 2** (bootstrap).
> 3. Seguir lineal hasta **Parte 4.5** (smoke test): infra operativa y un
>    job de Batch que entrena POP end-to-end.
> 4. Después **Partes 5-7** para CI/CD + promotion (no son indispensables
>    el día 1 pero conviene activarlos en semana 2).
> 5. **Parte 8** se usa como manual cuando algo falla; **Parte 11** es
>    la primera consulta cuando ves un error.
>
> Mantenimiento de esta guia: cada vez que cambies un modulo
> Terraform o un playbook, actualizar la seccion correspondiente +
> registrar el cambio en Apendice C (changelog V2.x).

---

# Parte 13 — Customizaciones puntuales (patches PROPUESTOS, no aplicados aun)

> **STATUS**: los 5 patches de esta parte estan **todos APLICADOS post-auditoria 2026-05-18**;
> mantener documentacion para historicidad y como referencia para
> futuros forks que quieran adoptarlos selectivamente. Si queres
> re-aplicar alguno tras cambios, segui las instrucciones del
> sub-bloque y corre `task infra:apply TARGET=module.X` correspondiente.
>
> **Por que esta parte existe**: las Partes 1-12 son la guia generica
> que sirve para cualquier deployment. Esta Parte 13 documenta **5
> patches** que podes aplicar a tu caso de uso particular:
>
> 1. Scheduler L/Mi/V (no L-V) — Sec 13.1 — **no aplicado**
> 2. Auto-train on push con wake/sleep — Sec 13.2 — **no aplicado** (workflow `auto-train-on-push.yml` no existe)
> 3. Orden serializado de wake (RDS → MLflow → Reports) — Sec 13.3 — **no aplicado** (scheduler.py sigue paralelo)
> 4. URLs locales documentadas (para que el dev sepa donde mirar local vs prod) — Sec 13.4 — solo doc
> 5. Como otro proyecto (FastAPI + Streamlit) consume este MLflow — Sec 13.5 — **modulo consumer-iam no creado**
> 6. MLflow local→prod (override del compose) — Sec 13.8 — **parcial** (codigo soporta override via `MLFLOW_TRACKING_URI`, falta `docker-compose.override.yml`)
>
> Aplicarlos DESPUES de que Oleadas 1-5 esten funcionando. Cada patch
> es independiente; podes adoptar 0, 1, varios o todos. Si los aplicas
> en mitad de un stand-up, podes dejar el state Terraform inconsistente.

## 13.1 Scheduler L/Mi/V (en vez de L-V)

> **STATUS: APLICADO** (commit despues de auditoria 2026-05-18). workdays_cron default ahora "MON,WED,FRI", Lambda recibe WORKDAYS_CRON+WORK_START_UTC+WORK_END_UTC, scheduler.py::_keepstop parametriza via _parse_workdays. Re-apply: `task infra:apply TARGET=module.scheduler`.

**Por que se cambia**: pediste que solo encienda lunes, miercoles y
viernes — no martes ni jueves. Reduce el uso de Fargate + RDS a
~48h/mes (3 dias × 4h × 4 semanas) en vez de 80h/mes. Ahorro de
costo: ~$3/mes en Fargate + ~$0.60 en RDS = **~$3.60/mes menos** (la
diferencia exacta esta en Sec 13.6).

### 13.1.1 Patch a `infra/modules/scheduler/variables.tf`

Editar el default:

```hcl
variable "workdays_cron" {
  type    = string
  default = "MON,WED,FRI" # antes: "MON-FRI". Ahora: lunes, miercoles, viernes.
}
```

### 13.1.2 Propagar `WORKDAYS_CRON` al Lambda — patch a `infra/modules/scheduler/main.tf`

Las dos rules de EventBridge (`start`, `stop`) usan `var.workdays_cron`
y ya respetan el cambio. **Pero** el `keepstop` rule corre cada 6h y su
defensa (no parar RDS si estamos dentro de horario laboral) tiene la
lista de dias **hardcodeada en `scheduler.py`** (`weekday < 5`).
Despues de este patch los martes/jueves quedan "dentro de ventana"
en el codigo y el RDS nunca se re-para -> se rompe el ahorro.

Agregar `WORKDAYS_CRON` al bloque `environment.variables` del Lambda:

```hcl
# infra/modules/scheduler/main.tf — bloque aws_lambda_function.scheduler
  environment {
    variables = {
      PROJECT            = var.project
      ECS_CLUSTER        = var.ecs_cluster_name
      ECS_SVC_MLFLOW     = var.ecs_service_name_mlflow
      ECS_SVC_REPORTS    = var.ecs_service_name_reports
      RDS_INSTANCE       = var.rds_instance_id
      JOB_QUEUE_SPOT     = "${var.project}-job-queue-spot"
      JOB_QUEUE_ONDEMAND = "${var.project}-job-queue-ondemand"
      WORKDAYS_CRON      = var.workdays_cron  # NUEVO: propagar al keepstop
      WORK_START_UTC     = tostring(local.start_hour_utc)  # NUEVO
      WORK_END_UTC       = tostring(local.stop_hour_utc)   # NUEVO
    }
  }
```

### 13.1.3 Patch a `infra/lambdas/scheduler.py::_keepstop`

Reemplazar la heuristica hardcoded por la lista de dias parseada desde
la env var. **Borrar** la version vieja de §3.10.4 y poner:

```python
# Mapeo de tokens de EventBridge cron a tm_wday (0=Monday)
_WEEKDAY_MAP = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}

def _parse_workdays(cron_token: str) -> set[int]:
    """Parsea 'MON,WED,FRI' o 'MON-FRI' a un set de tm_wday."""
    cron_token = cron_token.strip().upper()
    if "-" in cron_token:
        a, b = cron_token.split("-", 1)
        ia, ib = _WEEKDAY_MAP[a.strip()], _WEEKDAY_MAP[b.strip()]
        return set(range(ia, ib + 1))
    return {_WEEKDAY_MAP[tok.strip()] for tok in cron_token.split(",") if tok.strip()}


def _keepstop():
    """Defense: si RDS quedo RUNNING fuera de ventana, re-pararlo."""
    log.info("=== KEEPSTOP ===")
    workdays = _parse_workdays(os.environ.get("WORKDAYS_CRON", "MON-FRI"))
    start_utc = int(os.environ.get("WORK_START_UTC", "13"))
    end_utc = int(os.environ.get("WORK_END_UTC", "17"))

    utc_hour = time.gmtime().tm_hour
    weekday = time.gmtime().tm_wday
    in_window = (weekday in workdays) and (start_utc <= utc_hour < end_utc)
    if in_window:
        log.info("dentro de ventana (UTC=%02d:00, weekday=%d, workdays=%s), skip",
                 utc_hour, weekday, sorted(workdays))
        return

    db = rds.describe_db_instances(DBInstanceIdentifier=RDS_INSTANCE)["DBInstances"][0]
    state = db["DBInstanceStatus"]
    if state == "available":
        running = _running_jobs()
        if running:
            log.warning("Batch jobs activos, skip keepstop")
            return
        rds.stop_db_instance(DBInstanceIdentifier=RDS_INSTANCE)
        log.info("rds re-stopped por keepstop")
    else:
        log.info("rds en estado %s (skip)", state)
```

> **Por que parametrizar tambien las horas (`WORK_START_UTC`/`WORK_END_UTC`)**:
> si manana cambias `work_start_hour_local = 9`, las dos rules
> EventBridge se actualizan via `local.start_hour_utc`, pero el
> `_keepstop` seguia con `13 <= utc_hour < 17` hardcoded. Mismo bug
> que el de workdays, pero por hora. Lo arreglamos de paso.

### 13.1.4 Aplicar

```bash
task infra:apply TARGET=module.scheduler
```

> Esto regenera `scheduler.zip` (porque `archive_file.scheduler`
> detecta el cambio en `scheduler.py`) y publica la nueva version
> del Lambda. Verificable en consola AWS: Lambda → Functions →
> `ml-training-scheduler` → Configuration → Environment variables →
> aparece `WORKDAYS_CRON=MON,WED,FRI`.

### 13.1.5 Verificar

```bash
# El cron expression resultante debe contener "MON,WED,FRI"
aws events describe-rule --name ml-training-start \
    --query 'ScheduleExpression' --output text
# Esperado: cron(0 13 ? * MON,WED,FRI *)
#                ^^   ^^^ horas UTC (13 = 08:00 PET)

aws events describe-rule --name ml-training-stop \
    --query 'ScheduleExpression' --output text
# Esperado: cron(0 17 ? * MON,WED,FRI *)

# La env var WORKDAYS_CRON llega al Lambda
aws lambda get-function-configuration --function-name ml-training-scheduler \
    --query 'Environment.Variables.WORKDAYS_CRON' --output text
# Esperado: MON,WED,FRI
```

**Por que UTC y no PET en el cron**: EventBridge no soporta timezones
en cron expressions, solo UTC. Por eso el modulo `scheduler` calcula
`local.start_hour_utc = (8 - (-5) + 24) % 24 = 13` automaticamente
desde `tz_offset_hours = -5`. Si pasas a UTC-4 (DST), cambiar el var.

### 13.1.6 Que pasa si el martes/jueves alguien necesita la UI

Wake manual:

```bash
task aws:wake
# 5-8 min hasta que el ALB responde 200
```

Al terminar de usarla, el cron del proximo dia de calendario (L/Mi/V
12:00 PET) la apaga automaticamente. Si querres apagar manualmente:

```bash
task aws:sleep
```

## 13.2 Auto-train on push con wake + cool-down + auto-stop

> **STATUS: APLICADO** (commit despues de auditoria 2026-05-18). El workflow auto-train-on-push.yml fue CREADO y luego ABSORBIDO en training.yml (ver §6.4 reescrita). La policy gha-train ahora incluye lambda:InvokeFunction sobre scheduler, rds:DescribeDBInstances, ecs:DescribeServices. Re-apply: `task infra:apply TARGET=module.cicd`.

**Por que se hace**: pediste que el flujo sea:

1. Pusheo codigo a `main`.
2. Sistema detecta el cambio.
3. Si MLflow + RDS estan apagados, los enciende (orden 13.3).
4. Corre el entrenamiento.
5. Espera 10 minutos despues de que termine (cool-down).
6. Apaga RDS + MLflow + Reports.

**Por que el cool-down de 10 minutos**: para que `mlflow.log_model`
termine de subir artifacts a S3 (puede haber lag), para que el
notifier pueda leer el log del job FAILED si fue el caso, y para que
vos tengas chance de mirar el dashboard de reports si revisas el
mail de SUCCEEDED en ese momento.

**Por que NO se hace todo en un solo workflow lineal sin separar
ci.yml + auto-train.yml**: ci.yml puede correr en PRs (sin train), y
los push a main que NO tocan codigo del trainer (ej. docs) no
necesitan re-entrenar. Por eso lo separo en 2 workflows con
`workflow_run` dependency.

### 13.2.1 Patch al modulo `cicd`: ampliar permisos de gha-train

El `gha-train` actual solo invoca `dispatcher`. Necesita tambien
invocar `scheduler` para hacer wake/stop.

**Editar `infra/modules/cicd/main.tf` — REEMPLAZAR el bloque
`resource "aws_iam_role_policy" "train"` completo** (no agregar
statements al existente; el statement de `lambda:InvokeFunction` cambia
de `Resource = scalar` a `Resource = [array]`, y se agregan 2
statements nuevos para `rds:DescribeDBInstances` y
`ecs:DescribeServices`). Pegar este reemplazo:

```hcl
resource "aws_iam_role_policy" "train" {
  role = aws_iam_role.train.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [
          # AGREGADO: invoke scheduler tambien (era solo dispatcher)
          "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project}-dispatcher",
          "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project}-scheduler"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["batch:DescribeJobs", "batch:ListJobs"]
        Resource = "*"
      },
      {
        # AGREGADO: para que el workflow pueda chequear estado RDS antes de wake
        Effect   = "Allow"
        Action   = ["rds:DescribeDBInstances"]
        Resource = "*"
      },
      {
        # AGREGADO: chequear estado de los services Fargate
        Effect   = "Allow"
        Action   = ["ecs:DescribeServices"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:GetLogEvents", "logs:DescribeLogStreams"]
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/batch/${var.project}*"
      }
    ]
  })
}
```

Aplicar:

```bash
task infra:apply TARGET=module.cicd
```

### 13.2.2 Nuevo workflow `.github/workflows/auto-train-on-push.yml`

```yaml
name: Auto-train on push

on:
  workflow_run:
    workflows: ["CI"]     # depende de ci.yml (que pushea la imagen a ECR)
    types: [completed]
    branches: [main]
  workflow_dispatch:       # tambien manual (override)
    inputs:
      varieties:
        description: 'Variedades CSV o "all"'
        required: true
        default: 'all'
        type: string
      tuning:
        description: 'Profile'
        required: true
        default: 'prod'
        type: choice
        options: [smoke, dev, prod, prod_xl]

permissions:
  id-token: write
  contents: read

concurrency:
  group: auto-train
  cancel-in-progress: false   # NO cancelar — un job de Batch ya corriendo costaria $$ tirado

jobs:
  detect-change:
    runs-on: ubuntu-latest
    if: github.event_name == 'workflow_dispatch' || github.event.workflow_run.conclusion == 'success'
    outputs:
      should_train: ${{ steps.check.outputs.should_train }}
      varieties:    ${{ steps.check.outputs.varieties }}
      tuning:       ${{ steps.check.outputs.tuning }}
    steps:
      - uses: actions/checkout@v4
        with:
          # fetch-depth=0 trae historia completa para usar
          # github.event.before vs github.sha (robusto ante push de N commits).
          # HEAD~1 solo cubre push de 1 commit; un push squash o un revert
          # multi-commit lo rompe.
          fetch-depth: 0
      - name: Decide si re-entrenar
        id: check
        env:
          BASE_SHA: ${{ github.event.before }}
          HEAD_SHA: ${{ github.sha }}
        run: |
          if [[ "${{ github.event_name }}" == "workflow_dispatch" ]]; then
            echo "should_train=true" >> $GITHUB_OUTPUT
            echo "varieties=${{ inputs.varieties }}" >> $GITHUB_OUTPUT
            echo "tuning=${{ inputs.tuning }}" >> $GITHUB_OUTPUT
            exit 0
          fi
          # On workflow_run (despues de CI success): chequear que el push
          # toco codigo del trainer, no solo docs. Usamos before..sha en vez
          # de HEAD~1..HEAD para soportar push de N commits.
          CHANGED=$(git diff --name-only "${BASE_SHA}" "${HEAD_SHA}")
          echo "Cambios:"; echo "$CHANGED"
          if echo "$CHANGED" | grep -qE '^(src/|main\.py|Dockerfile|requirements\.txt|scripts/)'; then
            echo "should_train=true"  >> $GITHUB_OUTPUT
            echo "varieties=all"      >> $GITHUB_OUTPUT
            echo "tuning=prod"        >> $GITHUB_OUTPUT
          else
            echo "should_train=false" >> $GITHUB_OUTPUT
            echo "::notice::Push no toco trainer (solo docs/infra/yml), skip train"
          fi

  wake-services:
    needs: detect-change
    if: needs.detect-change.outputs.should_train == 'true'
    runs-on: ubuntu-latest
    outputs:
      mlflow_was_up: ${{ steps.check.outputs.was_up }}
    steps:
      - name: Assume gha-train role
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_GHA_TRAIN_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}

      - name: Estado actual de MLflow
        id: check
        run: |
          if curl -fs -o /dev/null --max-time 5 "http://${{ vars.MLFLOW_ALB_DNS }}/health"; then
            echo "was_up=true"  >> $GITHUB_OUTPUT
            echo "::notice::MLflow ya esta UP, skip wake"
          else
            echo "was_up=false" >> $GITHUB_OUTPUT
            echo "::notice::MLflow DOWN, invocando scheduler.start"
          fi

      - name: Wake (scheduler.start) si esta down
        if: steps.check.outputs.was_up == 'false'
        run: |
          aws lambda invoke \
            --function-name ${{ vars.PROJECT }}-scheduler \
            --cli-binary-format raw-in-base64-out \
            --payload '{"action":"start"}' \
            /tmp/start.out
          cat /tmp/start.out

      - name: Esperar RDS available (cold start ~5 min)
        if: steps.check.outputs.was_up == 'false'
        run: |
          for i in $(seq 1 24); do
            STATUS=$(aws rds describe-db-instances \
                       --db-instance-identifier ml-training-mlflow \
                       --query 'DBInstances[0].DBInstanceStatus' --output text)
            echo "RDS=$STATUS"
            [[ "$STATUS" == "available" ]] && break
            sleep 30
          done
          [[ "$STATUS" == "available" ]] || (echo "::error::RDS no available tras 12 min"; exit 1)

      - name: Esperar ALB MLflow 200 (Fargate task healthy)
        if: steps.check.outputs.was_up == 'false'
        run: |
          for i in $(seq 1 30); do
            CODE=$(curl -fs -o /dev/null -w "%{http_code}" --max-time 5 \
                   "http://${{ vars.MLFLOW_ALB_DNS }}/health" || echo "000")
            echo "MLflow=$CODE"
            [[ "$CODE" == "200" ]] && break
            sleep 10
          done
          [[ "$CODE" == "200" ]] || (echo "::error::MLflow no respondio 200 tras 5 min"; exit 1)

  train:
    needs: [detect-change, wake-services]
    if: needs.detect-change.outputs.should_train == 'true'
    runs-on: ubuntu-latest
    timeout-minutes: 480
    outputs:
      job_id:     ${{ steps.submit.outputs.job_id }}
      job_status: ${{ steps.wait.outputs.job_status }}
    steps:
      - name: Assume gha-train role
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_GHA_TRAIN_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}

      - name: Submit job via dispatcher
        id: submit
        run: |
          PAYLOAD=$(jq -nc \
            --arg v "${{ needs.detect-change.outputs.varieties }}" \
            --arg t "${{ needs.detect-change.outputs.tuning }}" \
            '{varieties: $v, tuning: $t}')
          aws lambda invoke \
            --function-name ${{ vars.PROJECT }}-dispatcher \
            --cli-binary-format raw-in-base64-out \
            --payload "$PAYLOAD" \
            /tmp/out.json
          cat /tmp/out.json
          JOB_ID=$(jq -r '.body.jobId' /tmp/out.json)
          echo "job_id=$JOB_ID" >> $GITHUB_OUTPUT

      - name: Wait completion
        id: wait
        run: |
          JOB_ID=${{ steps.submit.outputs.job_id }}
          while true; do
            STATUS=$(aws batch describe-jobs --jobs $JOB_ID \
                     --query 'jobs[0].status' --output text)
            echo "$(date -u): job_id=$JOB_ID status=$STATUS"
            if [[ "$STATUS" == "SUCCEEDED" || "$STATUS" == "FAILED" ]]; then
              echo "job_status=$STATUS" >> $GITHUB_OUTPUT
              break
            fi
            sleep 30
          done
          [[ "$STATUS" == "SUCCEEDED" ]]   # exit 1 si FAILED

  cool-down-and-stop:
    needs: [wake-services, train]
    # if: always() — corremos aun si el train fallo (apagar igual lo woke)
    if: always() && needs.wake-services.result == 'success' && needs.wake-services.outputs.mlflow_was_up == 'false'
    runs-on: ubuntu-latest
    steps:
      - name: Assume gha-train role
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_GHA_TRAIN_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}

      - name: Cool-down (10 min para sync S3 + reading dashboards)
        run: |
          echo "::notice::Cool-down 600s antes de apagar"
          sleep 600

      - name: Stop (scheduler.stop)
        run: |
          aws lambda invoke \
            --function-name ${{ vars.PROJECT }}-scheduler \
            --cli-binary-format raw-in-base64-out \
            --payload '{"action":"stop"}' \
            /tmp/stop.out
          cat /tmp/stop.out
```

**Por que la condicion `needs.wake-services.outputs.mlflow_was_up == 'false'`
en el cool-down job**: si MLflow YA estaba arriba cuando el push llego
(por ejemplo, estamos dentro de la ventana L/Mi/V 08-12 PET), NO lo
apagamos porque tenemos que respetar la ventana de uso humana. Solo
apagamos lo que NOSOTROS prendimos.

### 13.2.3 Variables nuevas de GitHub a setear

Ya estan todas las que usa este workflow (`AWS_GHA_TRAIN_ROLE_ARN`,
`AWS_REGION`, `MLFLOW_ALB_DNS`, `PROJECT`). No agregar nada.

### 13.2.4 Verificar end-to-end

```bash
# Forzar un push minimo al trainer
echo "# trigger: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> src/__init__.py
git add src/__init__.py
git commit -m "chore: trigger auto-train"
git push origin main

# Mirar el progreso
gh run watch
```

Esperado:
1. `CI` corre, build + push imagen a ECR.
2. `Auto-train on push` arranca con `workflow_run` trigger.
3. Job `detect-change` ve que cambio `src/` -> should_train=true.
4. Job `wake-services` ve MLflow down -> invoca scheduler.start -> espera.
5. Job `train` submitea a Batch -> espera SUCCEEDED.
6. Job `cool-down-and-stop` espera 10 min -> invoca scheduler.stop.

Tiempo total esperado: ~75-90 min (5 min wake + 30-50 min train + 10 min cool-down + 5 min stop).

## 13.3 Orden serializado de wake (RDS → MLflow → Reports)

> **STATUS: APLICADO** (commit despues de auditoria 2026-05-18). scheduler.py::_start ahora secuencial (RDS available -> MLflow running -> Reports). Lambda timeout bumpeado a 900s. Re-apply: `task infra:apply TARGET=module.scheduler`.

**Por que se serializa**: en el `scheduler.py` original (Parte 3.10.4),
las 3 acciones de start se invocan en paralelo: el `ecs.update_service`
para MLflow se manda sin esperar a que RDS este available. Por como
funciona Fargate, el container MLflow va a intentar conectar a RDS,
fallar, reintentar (~30s retry) hasta que RDS este up. Funciona, pero:

- Genera errores `connection refused` en los logs del container.
- El healthcheck del task definition (3.5.2) tiene `startPeriod=60s`,
  pero si RDS tarda 5+ min, falla y ECS reinicia el task.
- Reports puede arrancar antes de que tenga sentido (no afecta nada
  porque no depende de RDS, pero queda visualmente raro).

**Patch a `infra/lambdas/scheduler.py` — REEMPLAZAR la funcion `_start`
completa**. Identifica el bloque `def _start():` ... `def _stop():` en
la version de §3.10.4 (~lineas 3398-3413 del scheduler.py original) y
**borralo entero** antes de pegar la nueva version. Si no borras la
vieja, Python toma la ultima definicion del archivo (la nueva) pero
queda codigo muerto que confunde el `git blame`:

```python
def _start():
    """Wake secuencial: RDS -> MLflow -> Reports.

    Por que serializar y no lanzar todo en paralelo:
    1. El container MLflow intenta conectar a RDS al arrancar. Si RDS
       no esta available, falla healthcheck startPeriod -> ECS lo
       reinicia. Costoso en tiempo.
    2. Reports depende de S3 (no de RDS o MLflow) pero su UI vacia es
       confusa si MLflow todavia esta cargando. Mejor secuencial.
    """
    log.info("=== START (secuencial: RDS -> MLflow -> Reports) ===")

    # Etapa 1: RDS
    db = rds.describe_db_instances(DBInstanceIdentifier=RDS_INSTANCE)["DBInstances"][0]
    if db["DBInstanceStatus"] == "stopped":
        rds.start_db_instance(DBInstanceIdentifier=RDS_INSTANCE)
        log.info("rds start_db_instance ack")

    # Esperar hasta available (max ~8 min)
    for i in range(48):
        db = rds.describe_db_instances(DBInstanceIdentifier=RDS_INSTANCE)["DBInstances"][0]
        state = db["DBInstanceStatus"]
        log.info("rds[%d]=%s", i, state)
        if state == "available":
            break
        time.sleep(10)
    else:
        raise RuntimeError(f"RDS no available tras 8 min (estado={state})")

    log.info("rds OK -> arrancando MLflow")

    # Etapa 2: MLflow Fargate
    ecs.update_service(cluster=ECS_CLUSTER, service=ECS_SVC_MLFLOW, desiredCount=1)
    log.info("ecs %s -> desiredCount=1", ECS_SVC_MLFLOW)

    # Esperar hasta running + healthy (max ~5 min)
    for i in range(30):
        svc = ecs.describe_services(cluster=ECS_CLUSTER, services=[ECS_SVC_MLFLOW])["services"][0]
        running = svc.get("runningCount", 0)
        log.info("mlflow[%d]: running=%d desired=%d", i, running, svc.get("desiredCount", 0))
        if running >= 1:
            break
        time.sleep(10)
    else:
        log.warning("MLflow no esta running tras 5 min, arrancamos reports igual")

    # Etapa 3: Reports Fargate (no espera, es no-bloqueante)
    ecs.update_service(cluster=ECS_CLUSTER, service=ECS_SVC_REPORTS, desiredCount=1)
    log.info("ecs %s -> desiredCount=1", ECS_SVC_REPORTS)
    log.info("=== START OK ===")
```

> **Importante**: la lambda tiene `timeout = 300` (5 min) en el modulo
> scheduler (3.10.2). Si RDS tarda 8 min, la Lambda se mata con timeout.
> **Patch necesario tambien al modulo**:

**Patch a `infra/modules/scheduler/main.tf`** — cambio puntual: solo
la linea `timeout` del bloque `aws_lambda_function.scheduler`. NO
reemplazar el bloque entero (perderias los cambios de 13.1.2 que
agregaron `WORKDAYS_CRON`, `WORK_START_UTC`, `WORK_END_UTC` al
`environment.variables`).

```hcl
# En el bloque aws_lambda_function.scheduler:
  timeout = 900 # antes: 300. Ahora: 15 min para cubrir RDS cold start (~8 min).
```

> **Verificable en consola AWS**: Lambda → Functions →
> `ml-training-scheduler` → Configuration → General configuration →
> Timeout debe decir `15 min 0 sec` despues del apply.

Aplicar:

```bash
task infra:apply TARGET=module.scheduler
```

### 13.3.1 Verificar

```bash
# Forzar wake manual
aws lambda invoke 
    --function-name ml-training-scheduler 
    --cli-binary-format raw-in-base64-out 
    --payload '{"action":"start"}' 
    /tmp/start.out

# Ver el log del scheduler
aws logs tail /aws/lambda/ml-training-scheduler --follow
# Esperado:
#   === START (secuencial: RDS -> MLflow -> Reports) ===
#   rds start_db_instance ack
#   rds[0]=starting
#   rds[1]=starting
#   ...
#   rds[N]=available
#   rds OK -> arrancando MLflow
#   ecs mlflow -> desiredCount=1
#   mlflow[0]: running=0 desired=1
#   ...
#   mlflow[M]: running=1 desired=1
#   ecs reports -> desiredCount=1
#   === START OK ===
```

## 13.4 URLs locales y produccion (referencia)

**Por que se documenta**: tenes 2 contextos donde estos endpoints
existen: tu maquina (docker-compose) y AWS (ALB). Confundirlos cuando
debugueas es un dolor.

| Endpoint | Local (docker-compose) | Produccion (AWS) |
|---|---|---|
| MLflow UI + API | `http://localhost:5000` | `http://<ALB-DNS>/` |
| Reports HTML | `http://localhost:8080/reports/` | `http://<ALB-DNS>/reports/` |
| Artifacts crudos | `http://localhost:8080/artifacts/` | `http://<ALB-DNS>/artifacts/` |
| MLflow API health | `http://localhost:5000/health` | `http://<ALB-DNS>/health` |
| Postgres MLflow backend | `postgres://mlflow:***@localhost:5432/mlflow` (interno a la red de compose) | `postgres://mlflow:***@<RDS-DNS>:5432/mlflow` (no exponer, esta en SG privado) |
| Artifact store | `s3://...` (mismo bucket que prod si `S3_MLFLOW_BUCKET` apunta a el) | `s3://ml-training-artifacts-XXXXXX/artifacts/` |

**Como obtener el ALB DNS de prod**:

```bash
ALB="$(terraform -chdir=infra/envs/prod output -raw alb_dns)"
echo "MLflow:    http://${ALB}"
echo "Reports:   http://${ALB}/reports/"
echo "Artifacts: http://${ALB}/artifacts/"

# Guardarlo en env var para reusar
export MLFLOW_ALB_DNS="$ALB"
```

**Como apuntar tu local al MLflow productivo** (sin levantar compose):

```bash
# Override de la URI en tu sesion (solo para ejecutar scripts mlflow puntuales)
export MLFLOW_TRACKING_URI="http://${ALB}"

# Verificar que el cliente lo respeta
python -c "import mlflow; print(mlflow.get_tracking_uri())"
```

Esto te deja correr `mlflow models search`, `mlflow runs list`, etc.
desde tu bash contra el MLflow de AWS sin necesidad de levantar
docker compose.

## 13.5 Consumir el MLflow productivo desde OTRO proyecto (FastAPI + Streamlit)

> **STATUS: APLICADO** (commit despues de auditoria 2026-05-18). Modulo `infra/modules/consumer-iam/` creado, cableado en envs/prod/main.tf como Capa 10. Variables consumer_org="abantodca" y consumer_repo="ml_serving" en terraform.tfvars. Output `consumer_role_arn` agregado. Re-apply: `task infra:apply TARGET=module.consumer_iam`.

**Por que esta seccion**: pediste preparar el terreno para que tu
proyecto de inferencia (FastAPI + Streamlit, en repo separado) pueda
cargar modelos `Production` del MLflow que esta corriendo aca.

### 13.5.1 Contrato de consumo

El otro proyecto solo necesita 3 cosas:

1. **MLflow Tracking URI**: `http://<ALB-DNS>/` (lo expones via env var).
2. **Credenciales AWS** que tengan permisos `s3:GetObject` sobre el
   bucket `ml-training-artifacts-XXXXXX` (para descargar el `.joblib`).
3. **Nombre del modelo en Registry**: `ml-training-POP`,
   `ml-training-JUPITER`, etc. (uno por variedad).

### 13.5.2 IAM: crear un rol "consumer" en este repo's Terraform

**Por que en este repo y no en el otro**: el otro proyecto consume,
pero los permisos son sobre RECURSOS que ESTE repo crea. Mantener el
control de acceso aca te deja revisar quien tiene acceso de un solo
vistazo.

Crear el modulo nuevo `infra/modules/consumer-iam/` con **3 archivos
separados** (cada bloque va a su propio archivo, no concatenar):

**Archivo 1 — `infra/modules/consumer-iam/variables.tf`:**

```hcl
variable "project" { type = string }
variable "artifacts_bucket_arn" { type = string }
variable "consumer_oidc_arn" { type = string }
variable "consumer_org" { type = string }
variable "consumer_repo" { type = string }
```

**Archivo 2 — `infra/modules/consumer-iam/main.tf`:**

```hcl
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_iam_role" "consumer" {
  name = "${var.project}-consumer"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.consumer_oidc_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${var.consumer_org}/${var.consumer_repo}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "consumer" {
  role = aws_iam_role.consumer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # READ-ONLY al bucket artifacts
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.artifacts_bucket_arn, "${var.artifacts_bucket_arn}/*"]
      }
    ]
  })
}
```

**Archivo 3 — `infra/modules/consumer-iam/outputs.tf`:**

```hcl
output "consumer_role_arn" { value = aws_iam_role.consumer.arn }
```

**Integracion en `envs/prod/`** (3 archivos a editar, indicando DONDE
dentro de cada uno):

**a) `infra/envs/prod/main.tf`** — agregar **al final del archivo**,
despues del bloque `module "cicd"`:

```hcl
# Capa 10: Consumer IAM (otro repo de FastAPI/Streamlit consume artifacts)
module "consumer_iam" {
  source = "../../modules/consumer-iam"

  project              = var.project
  artifacts_bucket_arn = module.storage.artifacts_bucket_arn
  consumer_oidc_arn    = data.aws_iam_openid_connect_provider.github.arn
  consumer_org         = var.consumer_org  # ej "abantodca"
  consumer_repo        = var.consumer_repo # ej "ml-serving"
}
```

**b) `infra/envs/prod/variables.tf`** — agregar **al final**, junto a
otras vars sin default:

```hcl
variable "consumer_org" { type = string }
variable "consumer_repo" { type = string }
```

**c) `infra/envs/prod/terraform.tfvars`** — agregar **al final**:

```hcl
consumer_org  = "abantodca"
consumer_repo = "ml-serving"
```

**d) `infra/envs/prod/outputs.tf`** — agregar al final, para poder
leer el ARN despues del apply:

```hcl
output "consumer_role_arn" { value = module.consumer_iam.consumer_role_arn }
```

Aplicar:

```bash
task infra:apply TARGET=module.consumer_iam
```

Output del ARN para pasarselo al otro proyecto:

```bash
terraform -chdir=infra/envs/prod output -raw consumer_role_arn
# arn:aws:iam::123456789012:role/ml-training-consumer
```

### 13.5.3 Network access

**Por que importa**: el MLflow ALB esta en una VPC privada con SG
abierta solo a `0.0.0.0/0:80`. Si el otro proyecto corre en:

- **Mismo AWS account, misma VPC**: lo accedes por DNS interno via
  service discovery (`mlflow.ml-training.local:5000`). Sin internet.
- **Mismo account, otra VPC**: VPC peering o Transit Gateway.
- **Otro account / on-prem / GitHub-hosted runner**: usa el ALB
  publico (HTTP 80). Recomendado: activar TLS (Parte 10.1) antes.

Para tu caso "otro proyecto consume desde fuera", lo mas simple es
usar el ALB publico:

```python
MLFLOW_TRACKING_URI = "http://<ALB-DNS>/"   # ya lo tenes
```

### 13.5.4 Snippet FastAPI que carga modelo Production

En tu repo `ml-serving`, agregar:

```python
# src/serving/model_loader.py
"""Carga modelos Production desde el MLflow Registry de ml-training."""

from __future__ import annotations

import os
from functools import lru_cache

import mlflow.sklearn
from mlflow.tracking import MlflowClient

MLFLOW_TRACKING_URI = os.environ["MLFLOW_TRACKING_URI"]   # "http://<ALB-DNS>/"
mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
_client = MlflowClient()


@lru_cache(maxsize=16)
def load_production_model(variety: str):
    """Carga el modelo en Production para esa variedad.

    Cache LRU para evitar re-descargar el .joblib en cada request.
    """
    model_name = f"ml-training-{variety}"
    versions = _client.get_latest_versions(model_name, stages=["Production"])
    if not versions:
        raise RuntimeError(f"No hay Production para {model_name}")

    uri = f"models:/{model_name}/Production"
    pipeline = mlflow.sklearn.load_model(uri)
    return pipeline, versions[0].version


# src/api.py (FastAPI endpoint)
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from src.serving.model_loader import load_production_model

app = FastAPI()


class PredictRequest(BaseModel):
    variety: str
    features: dict


@app.post("/predict")
def predict(req: PredictRequest):
    try:
        model, version = load_production_model(req.variety)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    import pandas as pd
    df = pd.DataFrame([req.features])
    pred = float(model.predict(df)[0])
    return {"variety": req.variety, "model_version": version, "prediction": pred}
```

### 13.5.5 Snippet Streamlit que muestra Registry + corre prediccion

```python
# streamlit_app.py
import os
import requests
import streamlit as st
from mlflow.tracking import MlflowClient

MLFLOW_URI = os.environ["MLFLOW_TRACKING_URI"]
SERVING_URL = os.environ.get("SERVING_URL", "http://localhost:8000")

st.title("ML Training — Inference UI")

@st.cache_data(ttl=300)
def list_production_models():
    c = MlflowClient(tracking_uri=MLFLOW_URI)
    out = []
    for variety in ["POP", "JUPITER", "VENTURA", "SEKOYA", "ALLISON", "STELLA"]:
        try:
            mvs = c.get_latest_versions(f"ml-training-{variety}", stages=["Production"])
            if mvs:
                out.append({"variety": variety, "version": mvs[0].version, "run_id": mvs[0].run_id})
        except Exception:
            pass
    return out

st.subheader("Modelos en Production")
prod_models = list_production_models()
st.dataframe(prod_models)

st.subheader("Probar prediccion")
variety = st.selectbox("Variedad", [m["variety"] for m in prod_models])
features = st.text_area("Features JSON", '{"feat1": 1.0, "feat2": 2.5}')
if st.button("Predecir"):
    import json
    res = requests.post(f"{SERVING_URL}/predict", json={"variety": variety, "features": json.loads(features)})
    st.json(res.json())

st.subheader("Dashboard de reports")
ALB = MLFLOW_URI.rstrip("/")
st.markdown(f"[Abrir /reports/{variety}/]({ALB}/reports/{variety}/)")
```

### 13.5.6 Variables de env que el otro proyecto necesita

| Variable | Ejemplo |
|---|---|
| `MLFLOW_TRACKING_URI` | `http://ml-training-alb-1234.us-east-1.elb.amazonaws.com/` |
| `AWS_DEFAULT_REGION` | `us-east-1` |
| `AWS_ROLE_ARN` (en GHA) | `arn:aws:iam::<account>:role/ml-training-consumer` |
| `SERVING_URL` (Streamlit -> FastAPI) | `http://localhost:8000` o el endpoint productivo de FastAPI |

### 13.5.7 Workflow CI del proyecto consumer (resumen)

En el `ml-serving` repo, `.github/workflows/deploy.yml`:

```yaml
permissions:
  id-token: write
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::<account>:role/ml-training-consumer
          aws-region: us-east-1

      # Ahora boto3 y mlflow.sklearn.load_model pueden descargar artifacts
      # ... (tu deploy a Fargate / Lambda / etc.)
```

### 13.5.8 Que mantener separado y que no

| Cosa | Donde vive | Por que |
|---|---|---|
| Trainer + Terraform + Task + GHA train | `ml_training` (este repo) | Owner del MLflow server, del Registry y de los modelos |
| FastAPI inference + Streamlit UI | `ml-serving` (otro repo) | Consumer; ciclo de vida distinto (cambios de UI no requieren re-train) |
| IAM role consumer | Modulo `consumer-iam` de **este** repo | El trust lo definimos del lado del owner del recurso |
| MLFLOW_TRACKING_URI del consumer | Variable de GH del repo consumer | Se setea con el output `alb_dns` de este repo |

**Como sincronizar el URI cuando rebuild cambia el DNS**: el rebuild
(§8.6) cambia el ALB DNS. Tras rebuild, exportar el nuevo DNS y
actualizar la variable del otro repo:

```bash
NEW_ALB="$(terraform -chdir=infra/envs/prod output -raw alb_dns)"

# Actualizar la variable en el repo ml-serving (requiere gh CLI auth en ambos repos)
gh variable set MLFLOW_TRACKING_URI -b "http://${NEW_ALB}/" -R abantodca/ml-serving
```

Si tenes muchos consumers, considera la Parte 10.1 (Route 53 + ACM)
para tener un DNS estable (`mlflow.tu-dominio.com`) que no cambie tras
rebuild.

## 13.6 Recalculo de costos con MON,WED,FRI

**Por que recalcular**: el numero de horas/mes baja de 80 (L-V) a
~48 (L,Mi,V).

| Item | L-V (Parte 9.1) | MON,WED,FRI | Delta |
|---|---|---|---|
| RDS db.t4g.micro (48h/mes) | $1.44 | $0.86 | -$0.58 |
| Fargate MLflow (48h) | $7.90 | $4.74 | -$3.16 |
| Fargate Reports (48h) | $1.97 | $1.18 | -$0.79 |
| **Total** | **~$68** | **~$63** | **-$5/mes** |

Resto de items (S3, ECR, ALB, NAT GW, etc.) no cambia porque son 24/7
o por evento.

## 13.7 Orden de aplicacion de los 5 patches

> **No es un script ejecutable** — son instrucciones manuales en orden
> de dependencia. Cada paso 1-3 requiere editar archivos a mano
> (siguiendo las secciones citadas), y luego corres los `terraform
> apply` del paso 4.

**Paso 1 — Patches a Terraform/Python** (ediciones a mano):

- §13.1.1: editar `infra/modules/scheduler/variables.tf`
  (`workdays_cron = "MON,WED,FRI"`).
- §13.1.2: editar `infra/modules/scheduler/main.tf` (agregar
  `WORKDAYS_CRON`, `WORK_START_UTC`, `WORK_END_UTC` al
  `environment.variables` del Lambda).
- §13.1.3: editar `infra/lambdas/scheduler.py` (reemplazar `_keepstop`).
- §13.2.1: editar `infra/modules/cicd/main.tf` (reemplazar bloque
  `aws_iam_role_policy.train` completo).
- §13.3: editar `infra/lambdas/scheduler.py` (reemplazar funcion
  `_start`) + `infra/modules/scheduler/main.tf` (timeout 300 → 900).

**Paso 2 — Workflow nuevo**:

- §13.2.2: crear `.github/workflows/auto-train-on-push.yml`.

**Paso 3 — Modulo nuevo**:

- §13.5.2: crear `infra/modules/consumer-iam/` (3 archivos:
  variables.tf, main.tf, outputs.tf) + editar 4 archivos en
  `infra/envs/prod/` (main.tf, variables.tf, terraform.tfvars,
  outputs.tf).

**Paso 4 — Apply Terraform** en orden de dependencia:

```bash
task infra:apply TARGET=module.scheduler   # recoge cambios de 13.1 + 13.3
task infra:apply TARGET=module.cicd        # recoge cambios de 13.2.1
task infra:apply TARGET=module.consumer_iam # modulo nuevo de 13.5.2
```

> Si solo editaste `scheduler.py` sin tocar el `.tf`, Terraform detecta
> el cambio de hash via `archive_file.scheduler` y re-empaca el zip
> automaticamente. Si no detecta, forzar con
> `terraform -chdir=infra/envs/prod apply -replace=module.scheduler.aws_lambda_function.scheduler`.

**Paso 5 — Verificar**:

```bash
# Crons actualizados a MON,WED,FRI
aws events describe-rule --name ml-training-start --query 'ScheduleExpression' --output text
aws events describe-rule --name ml-training-stop  --query 'ScheduleExpression' --output text
# Esperado: cron(0 13 ? * MON,WED,FRI *) y cron(0 17 ? * MON,WED,FRI *)

# Lambda con timeout 900 + env vars nuevas
aws lambda get-function-configuration --function-name ml-training-scheduler \
    --query '[Timeout, Environment.Variables.WORKDAYS_CRON]' --output text
# Esperado: 900   MON,WED,FRI

# Rol consumer creado
aws iam get-role --role-name ml-training-consumer --query 'Role.Arn' --output text
```

> **En consola AWS veras**:
> - EventBridge → Rules → `ml-training-start`/`-stop` con la expresion
>   cron actualizada.
> - Lambda → `ml-training-scheduler` → Configuration → Timeout=15min
>   y env vars `WORKDAYS_CRON`/`WORK_START_UTC`/`WORK_END_UTC` nuevas.
> - IAM → Roles → `ml-training-consumer` (nuevo).
> - IAM → `ml-training-gha-train` → Permissions → policy con
>   `lambda:InvokeFunction` en array de 2 (dispatcher + scheduler).

**Paso 6 — Smoke end-to-end**: probar auto-train via push trivial (ver
§13.2.4).

---

## 13.8 Local + S3 + Produccion — reutilizar el setup sin perder runs locales

> **STATUS: APLICADO**. docker-compose.yml usa `MLFLOW_TRACKING_URI: ${MLFLOW_TRACKING_URI:-http://mlflow:5000}` (override-friendly). Se agrego `docker-compose.override.yml.example` como template (el override real no se commitea, esta en .gitignore).

**Por que se documenta**: el `docker-compose.yml` actual ya usa S3
(`S3_MLFLOW_BUCKET` para artifacts de MLflow + `S3_ARTIFACTS_BUCKET`
para el `s3_sync` post-run). Eso quiere decir que **cuando entrenas
en tu laptop, los modelos y reports ya viajan a S3** — el setup es
hibrido, no puramente local. Esta seccion documenta:

1. Que pieza del entrenamiento local **si** se persiste en cloud y
   cual **no**.
2. Como pasar el mismo compose a produccion (EC2/ECS) cambiando lo
   minimo.
3. Como dejar que tu laptop entrene contra el MLflow de prod para
   que **ningun run local quede huerfano**.

### 13.8.1 Mapeo del setup actual (que persiste y que no)

Lectura cruzada de `docker-compose.yml` + `scripts/s3_sync.py` +
`.env`:

| Pieza | Donde vive en local | Donde va a S3 |
|---|---|---|
| MLflow artifacts (joblib, plots, signatures) | Va directo a S3 (no toca disco local) | `s3://${S3_MLFLOW_BUCKET}/artifacts/` via `--default-artifact-root` en `docker-compose.yml:66` |
| MLflow tracking metadata (runs, params, metrics, experiments, run_id) | **Postgres en volumen Docker `pg-data`** (`docker-compose.yml:20`, `:131-132`) | **NO se sube a ningun lado** |
| `./artifacts/` del trainer (joblib, run_summary, champion JSON) | Bind mount `./artifacts:/app/artifacts` (`docker-compose.yml:122`) | `s3://${S3_ARTIFACTS_BUCKET}/artifacts/` via `scripts/s3_sync.py:81` |
| `./reports/` (HTML, xlsx) | Bind mount `./reports:/app/reports` (`docker-compose.yml:123`) | `s3://${S3_ARTIFACTS_BUCKET}/reports/` via `scripts/s3_sync.py:89` |
| Credenciales AWS | `~/.aws:/aws:ro` montado en mlflow + trainer (`docker-compose.yml:52`, `:119`) | — |

> **El unico dato que se queda atrapado en local es el Postgres de
> MLflow**. Los `.joblib` y los reports siempre llegan a S3. Lo que
> perderias al cambiar de laptop o levantar otro compose en otra
> maquina es el **contexto** (que run produjo que modelo, con que
> params, en que fecha).

Verificarlo en cualquier momento:

```bash
# Tras un run en local, los modelos deberian estar en S3
aws s3 ls s3://"$(grep ^S3_ARTIFACTS_BUCKET .env | cut -d= -f2)"/artifacts/
# Esperado: ver final_pipeline_*.joblib + run_summary_*.json del ultimo run

aws s3 ls s3://"$(grep ^S3_MLFLOW_BUCKET .env | cut -d= -f2)"/artifacts/ | head
# Esperado: directorios numerados de runs MLflow con sus artifacts
```

### 13.8.2 Diferencias minimas entre compose local y el deploy de prod

> **Aclaracion clave**: en produccion **no se usa `docker-compose`** —
> se usa ECS Task Definition (modulo `mlflow` de Parte 3) + Batch job
> definition para el trainer. El compose local y la task def de prod
> describen lo mismo (que container correr, con que env, que volumenes)
> pero en formatos distintos. La tabla compara equivalentes:

| Concepto | Local (compose) | Produccion (ECS Task Def / Batch) |
|---|---|---|
| **Credenciales AWS** | `~/.aws:/aws:ro` montado (`docker-compose.yml:52`, `:119`) + `AWS_SHARED_CREDENTIALS_FILE`/`AWS_PROFILE` | **Sin volumen** — IAM Task Role del Task Definition aporta las creds via metadata endpoint. El SDK las resuelve solo |
| **Backend store de MLflow** | Servicio `postgres` (compose:10-26) con volumen `pg-data` | RDS Postgres (modulo `rds` de Parte 3). El server MLflow recibe `--backend-store-uri postgresql://...@<RDS-DNS>:5432/mlflow` |
| **Servidor MLflow** | Container `mlflow` expuesto a `127.0.0.1:5000` | ECS Fargate detras de ALB (modulo `mlflow` de Parte 3) — el trainer apunta a `http://<ALB-DNS>` en vez de `http://mlflow:5000` |
| **Reports estaticos** | Servicio `reports` (nginx, compose:82-94) en `127.0.0.1:8080` | ECS Fargate con la misma imagen nginx detras del mismo ALB en path `/reports/` (modulo `reports` de Parte 3). Los archivos vienen de S3 (montados via `s3fs` o sincronizados en startup) |

> El **codigo del trainer no cambia** entre local y prod. `main.py:236`
> ya tiene `if S3_ARTIFACTS_BUCKET: sync_to_s3(...)`, asi que el mismo
> binario corre en ambos: con bucket configurado sube, sin bucket
> sigue de largo sin romper.

### 13.8.3 Estrategia para que NINGUN run local quede huerfano

La unica forma robusta de no perder el tracking de los runs locales
es **apuntar el MLflow de tu laptop al MLflow de produccion** (mismo
RDS, mismo bucket de artifacts). Esquema:

```
┌──────────────┐         ┌──────────────────────┐
│  Tu laptop   │────────▶│ MLflow Server (ECS)  │──┐
│  trainer     │  HTTPS  │ + RDS Postgres       │  │
│  (compose)   │         └──────────────────────┘  │
└──────────────┘                                    ▼
┌──────────────┐                          ┌─────────────────┐
│  EC2 prod    │────────▶ mismo MLflow ──▶│  S3 artifacts   │
│  trainer     │                          └─────────────────┘
└──────────────┘
```

**Como configurarlo** — crear un `docker-compose.override.yml` (no
commitear, va en `.gitignore`) con el override de URI:

```yaml
# docker-compose.override.yml (LOCAL, apuntando a prod)
# Compose lo merge automaticamente con docker-compose.yml
services:
  trainer:
    environment:
      MLFLOW_TRACKING_URI: http://${MLFLOW_ALB_DNS}
      # Bucket de prod — SOBREESCRIBE el S3_ARTIFACTS_BUCKET del .env local.
      # Antes de correr, verificar que el bucket existe:
      #   aws s3 ls s3://ml-training-artifacts-${ACCOUNT_SUFFIX}
      S3_ARTIFACTS_BUCKET: ml-training-artifacts-${ACCOUNT_SUFFIX}

  # Desactivar los servicios que ya no necesitas en local
  mlflow:
    profiles: ["disabled"]
  postgres:
    profiles: ["disabled"]
```

Y en tu `.env` local agregar (sacando el ALB DNS de Terraform):

```bash
echo "MLFLOW_ALB_DNS=$(terraform -chdir=infra/envs/prod output -raw alb_dns)" >> .env
echo "ACCOUNT_SUFFIX=$(aws sts get-caller-identity --query Account --output text | tail -c 7)" >> .env
```

Resultado: corres `docker compose up trainer` desde tu laptop, el run
aparece en la UI de prod (http://<ALB-DNS>), los artifacts se quedan
en el bucket de prod, y no hay un Postgres local que se pueda perder.

> **Cuidado con permisos** — MLflow **NO usa IAM** (la API es HTTP plano
> sobre el ALB, no hay acciones IAM `mlflow:*`). Tu cliente local solo
> necesita:
> - **Acceso de red** al ALB de prod (mismo SG que el de los consumers
>   de §13.5.3 — abierto a `0.0.0.0/0:80` o restringido a tu IP/VPN).
> - **Creds AWS** con `s3:PutObject` + `s3:GetObject` sobre
>   `s3://ml-training-artifacts-*/artifacts/*` (porque el cliente sube
>   los `.joblib` directo a S3 cuando el server tiene
>   `--default-artifact-root s3://...`). El server MLflow es quien
>   escribe a RDS — tu cliente no toca el RDS.
>
> Para crear el IAM user/role de dev escritor, usar como **inspiracion**
> `infra/modules/consumer-iam/` (§13.5.2 — el consumer solo lee, tu
> dev-writer ademas escribe `s3:PutObject` con prefijo `artifacts/`).

### 13.8.4 Alternativas si el MLflow compartido no es viable

| Caso | Estrategia | Trade-off |
|---|---|---|
| **Quiero entrenar offline (avion, casa sin VPN)** | Mantener compose local completo + `MLFLOW_EXPERIMENT_PREFIX=local_` en `.env` local. Al volver online, replicar via `mlflow experiments csv` + `mlflow runs list --to-csv` + re-loguear contra el server de prod | Replicacion manual; los timestamps quedan re-escritos a la fecha de re-import |
| **Equipo con varios devs queriendo aislamiento** | MLflow compartido + `MLFLOW_EXPERIMENT_PREFIX=dev_<USER>_` por dev | Cada dev pollute el server de prod con sus experimentos — mitigar con lifecycle de S3 que borre `artifacts/dev_*` >30 dias |
| **No quiero exponer el MLflow productivo a Internet** | Dejar el ALB en SG privado + cliente local via VPN o SSM port-forward (`aws ssm start-session --target i-xxx --document-name AWS-StartPortForwardingSession ...`) | Mas operacion; requiere VPN/SSM configurado para cada dev |

> Si elegis **mantener Postgres local**, asumi que **cada `docker
> compose down -v` borra el tracking**. Los `.joblib` y los reports
> sobreviven en S3, pero los experimentos de la UI no. Es un trade-off
> aceptable solo si trataste a esos runs como ejecuciones throwaway.

### 13.8.5 Cuando un run local se considera "promovible" a prod

> **Estado actual del gate** (Parte 7): el gate de promocion se
> dispara **manualmente** via `promote.yml` con `-f model=... -f
> version=...` (linea 6900 aprox). El gate hoy NO escanea runs por
> tag — solo aplica (1) umbral de MAPE, (2) A/B vs el campeon
> Production, y (3) approval humano. Asi que el "filtro" de runs
> locales **lo hace el dev en el approval**, no el gate automatico.

**Si igual queres marcar los runs locales** (recomendado — facilita
ignorarlos al hacer `mlflow runs list` y deja huella de auditoria),
hay dos formas:

**Forma A — set_tag dentro del codigo** (el patron que ya usa el repo
en `src/orchestration/single_run.py` y `src/step_06_track/mlflow_registry.py`):

```python
# En src/orchestration/single_run.py, donde ya se setean tags
import os
mlflow.set_tags({
    "env": os.environ.get("ML_ENV", "local-dev"),
    "dev_user": os.environ.get("USER", "unknown"),
})
```

Despues, en tu `.env` local o por sesion:

```bash
# Por defecto, runs locales = local-dev (no promovibles)
export ML_ENV=local-dev

# Para un run que queres marcar como candidato (revisable a mano)
ML_ENV=candidate task train -- --varieties POP --tuning full
```

> **No existe** una env var nativa `MLFLOW_TAGS` que MLflow lea
> automaticamente. Las unicas que el cliente respeta son
> `MLFLOW_TRACKING_URI`, `MLFLOW_EXPERIMENT_NAME`, `MLFLOW_RUN_NAME`.
> Los tags se setean **siempre** via `mlflow.set_tag(s)` en codigo.

**Forma B — tag manual post-run desde CLI** (sin tocar codigo):

```bash
# Sacar el run_id del ultimo run y taggearlo
RUN_ID=$(mlflow runs search --experiment-name POP --order-by 'start_time DESC' --max-results 1 --view-type ACTIVE_ONLY -o json | jq -r '.[0].info.run_id')
mlflow runs set-tag --run-id "$RUN_ID" --key env --value local-dev
```

**Si en el futuro queres que el gate auto-filtre por tag**, editar
`.github/workflows/promote.yml` (Parte 7) para que rechace
`version` cuyo source run tenga `env=local-dev` — hoy no esta
implementado, es trabajo a futuro.

### 13.8.6 Verificacion (3 checks)

```bash
# Check 1: el MLFLOW_TRACKING_URI apunta a prod (no localhost)
docker compose run --rm trainer python -c \
  "import mlflow; print(mlflow.get_tracking_uri())"
# Esperado: http://<ALB-DNS> (no http://mlflow:5000)

# Check 2: run end-to-end aparece en la UI de prod
task train -- --varieties POP --tuning smoke
open "http://$(terraform -chdir=infra/envs/prod output -raw alb_dns)"
# Esperado: el run mas reciente en el experiment "POP" (o el prefijo
# que tengas en MLFLOW_EXPERIMENT_PREFIX). Si aplicaste Forma A de
# 13.8.5, ademas veras tags env=local-dev + dev_user=$USER

# Check 3: artifacts en el bucket de prod
aws s3 ls "s3://ml-training-artifacts-${ACCOUNT_SUFFIX}/artifacts/" \
  --recursive | tail -5
# Esperado: final_pipeline_POP_*.joblib del run que acabas de correr
```

> **En consola AWS veras**:
> - MLflow UI (via ALB) → experiments → tu run mas reciente (con
>   tags `env=local-dev` + `dev_user=<vos>` solo si aplicaste la
>   Forma A o B de 13.8.5).
> - S3 → `ml-training-artifacts-XXXXXX/artifacts/` → joblib + JSONs
>   del ultimo run con timestamp reciente.
> - RDS Postgres → no se toca a mano (el server MLflow hace los
>   inserts).

---

> **Fin de la Parte 13 — customizaciones aplicadas.**
>
> Estado final con los patches:
> - Scheduler L/Mi/V 08-12 PET.
> - Push a main que toque `src/`, `main.py`, `Dockerfile`, `requirements.txt`
>   o `scripts/` -> auto-train con wake + train + cool-down 10 min + stop.
> - Wake serializado: RDS -> MLflow -> Reports.
> - Otro proyecto FastAPI/Streamlit con role IAM dedicado, snippet de
>   `load_production_model` y configuracion de env vars.
> - Trainer local apunta al MLflow de prod via `docker-compose.override.yml`
>   y los runs locales se taggean `env=local-dev` para no auto-promoverse.
> - Costo: ~$63/mes (5 menos que el default L-V).

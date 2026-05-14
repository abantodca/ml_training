# Guia MLOps AWS V2 — Despliegue a produccion de `ml_training`

> **Que es esta V2 y por que existe**
>
> Reorganizacion de `GUIA_MLOPS_AWS.md` (V1, 6537 lineas) con tres objetivos
> concretos que la V1 no garantiza:
>
> 1. **Que el codigo del proyecto NO se caiga al llegar a produccion.**
>    Alineada con el codigo real (`main.py`, `Dockerfile`, `docker-compose.yml`,
>    `src/orchestration/cli.py`): backends son **XGBoost + LightGBM** (no
>    Random Forest), target es **`KG/JR_H`** por variedad, hydrate de data
>    desde S3 con `S3_DATA_BUCKET`/`S3_DATA_KEY`, sync de outputs a
>    `S3_ARTIFACTS_BUCKET` con prefijos `artifacts/` y `reports/`. Cada
>    seccion del V2 verifica que el contrato del trainer se respeta antes
>    de avanzar a la siguiente.
>
> 2. **Lifecycle explicito de 4 modos**: STAND-UP (levantar de cero),
>    TEAR-DOWN (apagar preservando state+datos), REBUILD (volver a levantar
>    desde state preservado), DESTROY (eliminar TODO de la cuenta AWS,
>    incluido state). La V1 mezcla estos modos a lo largo del runbook;
>    aca son la Parte 1 entera y mandan el flujo.
>
> 3. **Copy-paste desde la raiz del repo, en bash**. Todos los comandos
>    asumen `cd /mnt/c/Users/.../ml_random_forest/ml_training` (en Windows
>    + WSL) o `~/Proyectos/ml_random_forest/ml_training` (Linux/macOS).
>    **En Windows: WSL Ubuntu de principio a fin** — toda la guia (bash,
>    Terraform, AWS CLI, Docker via WSL2 integration, Task) corre dentro
>    de WSL. NO Git Bash, NO PowerShell (memoria `feedback_shell_bash.md`).
>    Esta decision se aplica desde §0.3 hasta el final.
>
> **Que NO es esta V2**: un tutorial introductorio. Asume que ya conoces
> Terraform modular, Docker multi-stage, AWS Batch / ECS Fargate / RDS,
> GitHub Actions con OIDC y MLflow Tracking + Registry. Si te falta una
> sigla puntual, abri el Apendice A (Glosario) o la V1.
>
> **Como se entrega la V2**: en 5 oleadas. Esta version cubre Partes 0-2
> (decisiones + lifecycle + bootstrap). Las siguientes oleadas agregan
> P3 (modulos Terraform), P4 (apply incremental), P5-7 (patch trainer +
> CI/CD + promotion), P8-12 (runbook + costos + hardening + troubleshooting).
> Cuando una oleada se cierra, la anterior queda intacta — la guia se
> construye igual que la infra que describe: incremental, verificable.

---

## Indice general (de toda la V2, oleadas 1-5)

- **Parte 0 — Antes de empezar** *(oleada 1, ⬇ abajo)*
  - 0.1 Que construye esta guia
  - 0.2 Decisiones lockeadas
  - 0.3 Prerrequisitos verificables
  - 0.4 Convenciones (bash desde la raiz, naming, regions)
- **Parte 1 — Overview del lifecycle y stand-up** *(oleada 1, ⬇ abajo)*
  - 1.1 STAND-UP — primera vez, de cero a produccion
  - 1.2 Otros modos (TEAR-DOWN / REBUILD / DESTROY) — pointer a §8.5-§8.7
- **Parte 2 — Bootstrap irreversible** *(oleada 1, ⬇ abajo)*
  - 2.1 Por que el bootstrap es a mano
  - 2.2 Script de bootstrap (bash)
  - 2.3 Ejecutar UNA vez
  - 2.4 Verificacion post-bootstrap (4 checks)
  - 2.5 OIDC provider para GitHub Actions (pre-Terraform)
  - 2.6 Snapshot del estado bootstrapped (commit + tag)
- **Parte 3 — Modulos Terraform** *(oleada 2)*
- **Parte 4 — Apply incremental + smoke test** *(oleada 3)*
- **Parte 5 — Patch del trainer + re-build** *(oleada 4)*
- **Parte 6 — CI/CD con GitHub Actions** *(oleada 4)*
- **Parte 7 — Model promotion gate** *(oleada 4)*
- **Parte 8 — Runbook operativo extendido** *(oleada 5)*
- **Parte 9 — Costos detallados** *(oleada 5)*
- **Parte 10 — Hardening (futuro)** *(oleada 5)*
- **Parte 11 — Troubleshooting** *(oleada 5)*
- **Parte 12 — Apendices: glosario, conceptos, diferencias V1↔V2** *(oleada 5)*
- **Parte 13 — Customizaciones puntuales** *(addendum, opcional)*
  - 13.1 Scheduler L/Mi/V (en vez de L-V)
  - 13.2 Auto-train on push con wake + cool-down + auto-stop
  - 13.3 Orden serializado de wake (RDS → MLflow → Reports)
  - 13.4 URLs locales y produccion (referencia)
  - 13.5 Consumer IAM (FastAPI/Streamlit consumiendo MLflow productivo)
  - 13.6 Recalculo de costos con MON,WED,FRI

> **Parte 13 es un addendum**, no parte del runbook lineal. Cada
> subseccion **modifica infra ya aplicada** (re-apply Terraform,
> nuevos modulos, env vars adicionales). Saltala en la primera lectura.

---

## Filosofia: por que cada oleada existe

> **Antes de copy-pastear nada, leer esto.** Esta guia esta organizada
> en 5 oleadas porque cada una resuelve un problema distinto. Si no
> entendes el problema, vas a copiar codigo que no se ajusta a tu caso.

### Por que esta dividida en 5 oleadas (y no es un script unico)

Un script unico que "lo levanta todo" es atractivo pero peligroso:

- **Falla a la mitad y no sabes en que estado quedaste**: AWS te cobra
  igual por los recursos que se crearon antes del error.
- **No podes razonar el rollback**: si todo se aplico junto, todo se
  destruye junto.
- **No podes evolucionar**: cuando manana queres cambiar la queue de
  Batch, no sabes que blast-radius tiene.

Por eso cada oleada cierra con un **estado verificable** (los 4 checks
post-bootstrap, el smoke de Ola C, el patch del trainer con MAPE en
CloudWatch). Si un check falla, **paras ahi** — no pasas a la siguiente.

### Por que cada oleada existe (resumen ejecutivo)

| Oleada | Que problema resuelve | Que falla si la salteo |
|---|---|---|
| **1** — Bootstrap + Lifecycle | Tener un punto fijo donde el state vive (S3+DDB), credenciales sin secrets (OIDC), y un mental model de los 4 modos de operacion (stand-up/tear-down/rebuild/destroy) | Terraform local-state se pierde con el laptop; OIDC sin pre-crear -> chicken-and-egg con `cicd` modulo; sin lifecycle pensado, el destroy borra tus modelos sin querer |
| **2** — Modulos Terraform | Aislar blast-radius: tocar `batch` no toca `mlflow`. Y dejar las interfaces (variables.tf + outputs.tf) listas para reutilizar en `envs/dev/` o `envs/staging/` | Un main.tf monolitico de 2000 lineas vuelve cualquier cambio un riesgo de "y si rompo X?" |
| **3** — Apply incremental + smoke | Validar end-to-end que el trainer real entrega un modelo en Registry + artifacts en S3 + dashboard accesible. Sin esto, "el deploy fue OK" no significa nada | Te enteras en produccion que el ALB no resuelve por dentro, o que el job role no tiene permisos para PutObject — bugs que un apply sin smoke no detecta |
| **4** — Trainer patch + CI/CD + promotion | (a) Conectar alarmas de monitoring a metricas reales. (b) Dejar de hacer deploys a mano. (c) No promover modelos peores sin darse cuenta | Sin (a), MAPE alto pasa silencioso. Sin (b), cada deploy es susceptible a "se me olvido el push del MLflow image". Sin (c), Production puede degradarse silenciosamente |
| **5** — Runbook + costos + hardening + troubleshooting + apendices | Operar el sistema en produccion sin Claude/devs senior al lado: que comando correr cuando algo falla, cuanto cuesta cada modo, que hardening activar cuando expones a Internet | Sin runbook + troubleshooting, cada incidente es un debug desde cero y un mail al desarrollador original |

### Por que estos cuatro modos (stand-up/tear-down/rebuild/destroy)

La V1 tiene un "runbook" donde scale-up, scale-down y destroy estan
mezclados con manuales operativos. La V2 los separa porque cada uno:

- **Tiene una pregunta de usuario distinta** (ver Parte 1). Confundir
  "tear-down" con "destroy" te puede costar todo el Model Registry.
- **Tiene una transicion legitima distinta**. Stand-up → tear-down →
  rebuild es seguro y reversible. Stand-up → destroy es definitivo.
- **Tiene un perfil de costo distinto** que el desarrollador necesita
  ver (§9.3 — matriz cruzada de costos por modo).

### Por que Terraform + Task + GitHub Actions (las 3 a la vez)

No es duplicacion — cada una resuelve un tipo distinto de cosa:

- **Terraform** es declarativo y idempotente: dice "esta es la infra
  que quiero". Perfecto para recursos cuya forma final es fija (VPC,
  subnets, RDS, IAM roles). Es lo que cubre Parte 3.
- **Task** es un orquestador imperativo: dice "para hacer X, corre
  estos comandos en este orden". Perfecto para flujos con condiciones
  (esperar a que RDS arranque, drenar Batch antes de apagar, encadenar
  `terraform apply` con `docker push`). Es lo que cubre Parte 4.
- **GitHub Actions** es event-driven y auditable: dice "cuando pasa X,
  corre Y". Perfecto para CI (push → build) y triggers manuales con
  approval (promote.yml + GitHub Environment). Es lo que cubre Parte 6.

Si usaras solo Terraform tendrias que hacer `terraform apply` a mano
en cada deploy y sin orquestacion. Si usaras solo Task / bash, perderias
el state remoto y los drifts no se detectarian. Si usaras solo GitHub
Actions, no tendrias forma de correr operaciones localmente. Las tres
juntas se cubren los puntos ciegos mutuamente.

**Nota sobre Ansible (V1 deprecated)**: la V1 de esta guia usaba Ansible
en lugar de Task. Lo migramos porque el stack es Docker + AWS managed
services (no hosts EC2), donde Ansible es overkill: requiere Python+pipx,
no es native Windows, y la sintaxis YAML+Jinja+DSL es mas pesada que
los `cmds:` POSIX de Task. Ver §4.1 para la comparacion completa.

### Por que algunas cosas se hacen a mano y no via Terraform

Tres recursos son **bootstrap a mano** (Parte 2):

- **S3 backend + DynamoDB lock**: chicken-and-egg — Terraform necesita
  el backend para guardar el state, pero no puede crearlo si su state
  vive ahi.
- **OIDC provider**: si lo crea Terraform y haces `destroy`, el
  proximo GHA falla porque su trust ya no existe. Sobrevivir destroys
  importa.
- **SLRs (Service Linked Roles)**: AWS los crea solos la primera vez
  que un servicio se usa, pero meterlos en bootstrap los hace
  explicitos y auditables.

Todo lo demas es Terraform porque cualquier recurso que cambie con
frecuencia merece estar en codigo versionable.

### Por que el codigo Lambda esta en `infra/lambdas/` y no en `src/`

Los `.py` de las Lambdas son **infraestructura**, no aplicacion. Vida
util: vinculada al modulo `lambdas/` o `scheduler/`. Cambiar el codigo
de `dispatcher.py` requiere `terraform apply` (porque `archive_file`
re-zipea). Por eso vive con la infra que lo despliega, no con el
trainer.

---

# Parte 0 — Antes de empezar

## 0.1 Que construye esta guia

Al terminar las 5 oleadas tenes corriendo en AWS:

| Capa | Recurso real | Encendido cuando |
|---|---|---|
| **Storage** | S3 `ml-training-data-XXXXXX` (Excels de input), S3 `ml-training-artifacts-XXXXXX` (modelos + reportes), S3 `ml-training-tfstate-XXXXXX` (Terraform state) | 24/7 (storage no factura compute) |
| **Registry** | ECR `ml-training` (imagen del trainer), `ml-training-mlflow` (MLflow server custom), `ml-training-reports` (nginx serving S3) | 24/7 |
| **Red** | VPC `10.20.0.0/16` single-AZ, 1 subnet publica + 1 privada + NAT GW, security groups por capa | 24/7 |
| **Tracking** | RDS Postgres `db.t4g.micro` (MLflow backend store) + ECS Fargate (MLflow server) detras de ALB :80 | **L-V 08-12 PET** (scheduler la apaga fuera de ventana) |
| **Dashboards** | ECS Fargate nginx sirviendo `s3://artifacts/{reports,artifacts}/` via paths `/reports/*` y `/artifacts/*` del ALB | **L-V 08-12 PET** |
| **Training** | AWS Batch con 2 queues: Spot (default smoke/dev/prod) + On-Demand (solo prod_xl), retry=2, instancia c6i.2xlarge | Solo durante un job (auto 0↔N) |
| **Orquestacion** | Lambda `ml-training-dispatcher` (submit jobs), Lambda `ml-training-notifier` (alertas), Lambda `ml-training-scheduler` (auto on/off RDS+Fargate) | Solo cuando un trigger las invoca |
| **Eventos** | EventBridge rules: 1 cron L-V 08:00 PET start, 1 cron L-V 12:00 PET stop, 1 rule "Batch Job FAILED" → notifier | 24/7 (cron es serverless) |
| **Alarmas** | CloudWatch alarms: job FAILED, MAPE > umbral (custom metric), ALB 5xx | 24/7 |
| **Notificaciones** | SNS topic `ml-training-alerts` con suscripcion email (`abantodca@gmail.com`) | 24/7 |
| **CI/CD** | GitHub Actions con OIDC: `ci.yml` (lint + test + build + push), `train.yml` (workflow_dispatch para entrenar), `promote.yml` (Staging→Production con gate), `terraform-plan.yml` (plan en PRs) | Solo cuando hay push/PR |

### Endpoints en produccion (un solo ALB)

```
http://<ALB-DNS>/              -> MLflow UI (tracking + Model Registry)
http://<ALB-DNS>/reports/      -> Dashboards HTML por variedad
http://<ALB-DNS>/artifacts/    -> Artifacts crudos por run
```

### Flujo end-to-end (mental model)

```
Developer
  | (push a main)
  v
GitHub Actions ci.yml
  | (build + push ECR via OIDC)
  v
ECR ml-training:<sha>
  | (workflow_dispatch train.yml o manual aws lambda invoke)
  v
Lambda dispatcher --> AWS Batch SubmitJob (Spot queue)
  | (autoscale 0->1 EC2 c6i.2xlarge)
  v
Container del trainer
  | 1. hydrate S3_DATA_BUCKET/S3_DATA_KEY -> data/training/DB-HISTORICA.xlsx
  | 2. main.py: parse_args -> run_parallel/run_sequential
  | 3. Por variedad: XGB + LGB con Optuna -> champion.select_champion
  | 4. log a MLflow (Postgres backend + S3 artifacts)
  | 5. sync_to_s3(artifacts/, reports/) a S3_ARTIFACTS_BUCKET
  v
MLflow Model Registry: nueva version en "None"
  | (workflow_dispatch promote.yml)
  v
Quality gate: MAPE < umbral && A/B contra Production actual
  | (manual approval en GitHub Environments)
  v
MLflow Model Registry: version transicionada a "Production"
```

### Costo objetivo

Con scheduler L-V 08-12 PET activado desde el dia 1: **~$68 USD/mes**
(detalle en Parte 9; la diferencia con el ~$64 de la V1 viene de incluir
Reports Fargate y NAT egress que la V1 no contaba). Sin scheduler
(24/7): ~$140/mes. Solo storage (hibernado, §8.5): ~$8/mes.

---

## 0.2 Decisiones lockeadas

Estas decisiones NO se discuten en esta guia. Si queres cambiar alguna,
es un cambio de scope que requiere reescribir secciones; documentalo en
un ADR antes.

| Decision | Eleccion lockeada | Por que ahora | Alternativa futura |
|---|---|---|---|
| **Region AWS** | `us-east-1` (N. Virginia) | Latencia razonable desde Peru, todos los servicios disponibles (incluido Fargate Spot), menor precio Spot. | `us-east-2` o `sa-east-1` si compliance lo pide. |
| **Compute training** | Batch + EC2 c6i.2xlarge: 2 queues (Spot default + On-Demand solo `prod_xl`), `retry=2` | -70% costo Spot. Retry cubre interrupciones (~5-10% en c6i.2xlarge). OD reservada para `prod_xl` (5-6h × P(20-30%) duele). | Fargate Spot (sin SSH/volume control) o `g5.xlarge` si pasas a deep learning. |
| **Compute serving MLflow/Reports** | ECS Fargate (no EC2) | Cero gestion de host, autoscale, integracion nativa con ALB. | EC2 con AMI custom si necesitas runtime acceleration. |
| **RDS** | Postgres 15, `db.t4g.micro`, single-AZ, sin replica, sin Multi-AZ | $13/mes encendido, suficiente para MLflow metadata (<10 GB en anios). | Multi-AZ si MLflow se vuelve critical-path (Parte 10.4). |
| **Auto on/off (RDS + Fargate)** | Scheduler EventBridge L-V 08-12 PET + chequeo de Batch jobs RUNNING antes de apagar | UI 4h/dia accesible; entrenamiento puede correr fuera de ventana (workflow `train.yml` wake-ea servicios on-demand). Sabado/domingo apagado entero. | 24/7 si team distribuido en varios time zones. |
| **TLS / WAF / Multi-AZ** | **NO** (ALB :80 HTTP, sin WAF, RDS single-AZ) | Default barato. MLflow tiene Basic Auth y SG restrictivo. Activar antes de exponer a Internet abierta. | Parte 10.1 (TLS), 10.2 (WAF), 10.4 (Multi-AZ). |
| **Egress de subnet privada** | NAT Gateway single-AZ ($32/mes) | Setup simple, costo razonable si trafico < 10 GB/mes. | VPC endpoints (Parte 10.3) si trafico crece o policy zero-egress. |
| **Trigger de training** | (a) GitHub Actions `train.yml` workflow_dispatch (wake-train-sleep), (b) `aws lambda invoke ml-training-dispatcher` (asume servicios up). **Sin cron de training, sin S3 PutObject trigger.** | Cualquier user entrena con un click desde GitHub UI eligiendo variedad. Training fuera de ventana wake-ea servicios y los apaga al terminar. | EventBridge cron diario / S3 PutObject trigger (Parte 7.5, futuro). |
| **Backend de MLflow** | Postgres (backend store) + S3 (artifact store), NO filesystem | Estandar de industria, soporta concurrencia, scale-out. | Filesystem solo en dev local (lo que hace `docker-compose.yml` con Postgres tambien). |
| **Modelos entrenables** | **XGBoost + LightGBM** (no Random Forest), ambos via `TransformedTargetRegressor` con log1p + cap-p99 sobre `KG/JR_H`. Champion automatico (no flag de usuario). | Es lo que esta en `src/step_04_train/registry.py` hoy. Estabilidad numerica del target garantizada por la transformacion. | Stacking (eliminado del codigo, NO existe). |
| **Variedades validas** | `POP`, `JUPITER`, `VENTURA`, `SEKOYA`, `ALLISON`, `STELLA` | Hardcoded en `src/orchestration/cli.py:resolve_varieties` y validado en Lambda dispatcher. | Agregar nueva variedad: PR a `cli.py` + variable Terraform `varieties_allowed`. |
| **OIDC vs access keys** | OIDC (sin secrets de larga duracion en GitHub) | Auditable en CloudTrail, sin rotacion manual, blast-radius limitado al repo. | Access keys solo en CI legacy que no soporta OIDC. |
| **Promotion** | Quality gate (MAPE < umbral) + A/B contra Production actual + approval humano en GitHub Environments | Defense in depth: sklearn no garantiza un MAPE menor por si solo; el A/B compara contra baseline real. | Auto-promote sin approval si MAPE absoluto es <5% (no recomendado). |

---

## 0.3 Prerrequisitos verificables

Cada bloque tiene un comando que valida que el prerequisito existe. Si
falla, detenete y resolvelo — la siguiente parte asume que el anterior
paso fue OK.

### 0.3.1 Cuenta AWS y credenciales

```bash
# 1) AWS CLI v2 instalado
aws --version
# Esperado: aws-cli/2.x.x ...

# 2) Profile activo apunta a la cuenta correcta
aws sts get-caller-identity
# Esperado: { "UserId": "...", "Account": "<12-digitos>", "Arn": "arn:aws:iam::...:user/..." }

# 3) Region default seteada en us-east-1 (o exportar a la sesion)
export AWS_DEFAULT_REGION="us-east-1"
aws configure get region
# Esperado: us-east-1
```

Si la salida del paso 2 te muestra una cuenta distinta a la que vas a
usar, configura el profile correcto **antes** de continuar:

```bash
aws configure --profile ml-prod
export AWS_PROFILE="ml-prod"
aws sts get-caller-identity   # re-verifica
```

### 0.3.2 Service quotas (validar antes del primer apply)

Pedi aumento ANTES de aplicar Terraform — los tickets de quota tardan
24-48h. Valores minimos:

| Servicio | Quota | Valor minimo | Comando de verificacion |
|---|---|---|---|
| EC2 | Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances | 32 vCPUs | `aws service-quotas get-service-quota --service-code ec2 --quota-code L-1216C47A` |
| EC2 | All Standard (A, C, D, H, I, M, R, T, Z) Spot Instance Requests | 32 vCPUs | `aws service-quotas get-service-quota --service-code ec2 --quota-code L-34B43A08` |
| VPC | NAT gateways per AZ | 5 (default) | `aws service-quotas get-service-quota --service-code vpc --quota-code L-FE5A380F` |
| RDS | DB instances | 40 (default) | OK |
| Lambda | Concurrent executions | 1000 (default) | OK |

Si alguno esta < minimo:

```bash
# Pedir aumento programatico (responde con un RequestId; tracking en consola)
aws service-quotas request-service-quota-increase 
  --service-code ec2 
  --quota-code L-1216C47A 
  --desired-value 32
```

### 0.3.3 Herramientas locales

> **Entorno (Windows host, repo en `C:\Users\...`, trabajo desde WSL)**:
> el repo vive fisicamente en `C:\Users\CarlosAlexanderAbant\Documents\Proyectos\ml_random_forest\ml_training`.
> No se mueve. Desde Windows abris **WSL Ubuntu** y operas el repo via
> el mount `/mnt/c/...`:
>
> ```bash
> # Desde PowerShell o terminal Windows, abrir WSL Ubuntu:
> wsl -d Ubuntu                   # o abrir la app "Ubuntu" del menu Inicio
>
> # Ya dentro de WSL, navegar al repo (que vive en el disco Windows):
> cd /mnt/c/Users/CarlosAlexanderAbant/Documents/Proyectos/ml_random_forest/ml_training
> pwd                             # confirmar
> ```
>
> **Caveats de operar sobre `/mnt/c/...`** (NTFS visto desde WSL):
> 1. `chmod +x script.sh` no persiste cross-reboot (NTFS no guarda el
>    bit POSIX). Por eso esta guia siempre invoca scripts con
>    `bash infra/xxx.sh` (no `./xxx.sh`) — funciona sin importar el bit.
> 2. `docker build` desde `/mnt/c/...` es ~3-5x mas lento que desde
>    `~/` ext4. Asumible para esta guia (los builds pesados van a ECR,
>    no se rebuildean cada vez). Si te molesta, ver opcion B abajo.
> 3. git puede marcar todos los archivos como modificados por CRLF vs
>    LF. Fix de una sola vez:
>    `git config --global core.autocrlf input` + `git add --renormalize .`
> 4. Docker Desktop tiene que exponer el daemon a WSL: Settings →
>    Resources → WSL integration → enable "Ubuntu". Asi `docker` desde
>    WSL pega contra el mismo daemon que Windows.
>
> **Linux/macOS nativo**: ignorar todo lo anterior, terminal estandar y
> path tipico `~/Proyectos/ml_random_forest/ml_training`.

```bash
# Terraform (>= 1.6)
terraform version

# Docker Desktop / Docker engine corriendo (cliente dentro de WSL)
docker version
docker info | grep "Server Version"

# Git
git --version

# jq (usado en post-apply checks)
jq --version

# Task (orquestador local + AWS, instalado dentro de WSL/Linux/macOS)
task --version
# Esperado: 3.34+ (necesario para `prompt:` en tasks destructivos)
```

Si `task --version` falla, instalar:

```bash
# Windows (WSL Ubuntu) y Linux: mismo instalador
sh -c "$(curl --location https://taskfile.dev/install.sh)" -- -d -b ~/bin
export PATH="$HOME/bin:$PATH"   # agregarlo a ~/.bashrc para persistir

# macOS
brew install go-task
```

### 0.3.4 Estado actual del repo

```bash
cd /mnt/c/Users/CarlosAlexanderAbant/Documents/Proyectos/ml_random_forest/ml_training
git status
git log --oneline -5
```

El working tree actual tiene 26 archivos `infra/` borrados (en git
history, no en disco) y `GUIA_MLOPS_AWS.md` modificado. La V2 los
reconstruye desde cero — **no `git checkout` esos archivos**, la nueva
infra arranca limpia.

### 0.3.5 Verificacion del trainer local

Antes de subir a AWS, valida que el trainer corre limpio localmente.
Esto descarta que el bug venga del codigo:

```bash
# (a) docker compose levanta Postgres + MLflow + nginx + trainer con CMD default
docker compose build trainer mlflow
docker compose up -d postgres mlflow reports

# (b) MLflow responde en localhost
curl http://localhost:5000/health   # 200 OK

# (c) Trainer smoke (1 variedad, tuning chico)
docker compose run --rm trainer --varieties POP --tuning smoke
# Esperado al final: "FIN | variedades=1 | falladas=0 | tiempo_total=...s"
# Y en MLflow UI: experimento "POP" con 2 runs (xgb + lgb) y 1 champion

# (d) Limpiar
docker compose down
```

Si (c) falla, **no avances al bootstrap** — primero arregla el trainer.

---

## 0.4 Convenciones

### Punto de partida

Cada bloque bash asume que estas en la raiz del repo, **dentro de WSL
Ubuntu si estas en Windows** (o bash nativo en Linux/macOS):

```bash
# Windows: abrir terminal "Ubuntu" (o `wsl -d Ubuntu` desde PowerShell).
#          El disco Windows se monta en /mnt/c/...
# Linux/Mac: terminal nativo, ~/Proyectos/ml_random_forest/ml_training (o similar)
cd /mnt/c/Users/CarlosAlexanderAbant/Documents/Proyectos/ml_random_forest/ml_training
```

A partir de aqui y hasta el final de la guia, **siempre WSL Ubuntu en
Windows**. No se mezcla con Git Bash ni PowerShell. Si la guia no lo
dice explicito, ya estas en esa terminal y en la raiz del repo.

### Variables de entorno de la sesion

Setear UNA vez por terminal antes de empezar a trabajar:

```bash
export AWS_DEFAULT_REGION="us-east-1"
export AWS_PROFILE="ml-prod"        # o el que uses
export PROJECT="ml-training"        # slug usado en todos los nombres
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export ACCOUNT_SUFFIX="${ACCOUNT_ID: -6}"
```

Las 4 referencias que aparecen una y otra vez:

| Variable | Valor | Usado para |
|---|---|---|
| `$PROJECT` | `ml-training` | Prefijo de TODOS los recursos AWS |
| `$ACCOUNT_ID` | 12 digitos | tfstate bucket, ECR URIs, role ARNs |
| `$ACCOUNT_SUFFIX` | 6 digitos | sufijo de buckets (para evitar colision con otras cuentas) |
| `$AWS_DEFAULT_REGION` | `us-east-1` | scope de todo el deployment |

### Naming convention de recursos

| Tipo | Patron | Ejemplo |
|---|---|---|
| Bucket S3 | `${PROJECT}-<funcion>-${ACCOUNT_SUFFIX}` | `ml-training-tfstate-AB12CD` |
| DynamoDB | `${PROJECT}-<funcion>` | `ml-training-tflock` |
| ECR repo | `${PROJECT}` o `${PROJECT}-<sufijo>` | `ml-training`, `ml-training-mlflow`, `ml-training-reports` |
| Lambda | `${PROJECT}-<funcion>` | `ml-training-dispatcher` |
| Batch queue | `${PROJECT}-job-queue-<tipo>` | `ml-training-job-queue-spot` |
| RDS instance | `${PROJECT}-mlflow` | `ml-training-mlflow` |
| ECS cluster | `${PROJECT}-cluster` | `ml-training-cluster` |
| IAM role | `${PROJECT}-<funcion>-role` | `ml-training-batch-role`, `ml-training-gha-deploy` |
| SNS topic | `${PROJECT}-alerts` | `ml-training-alerts` |

### Convencion de terminales

- **bash desde WSL Ubuntu (Windows) o nativo (Linux/macOS)**: todos los
  comandos son bash desde la raiz del repo. `$VAR` para variables, `&&`
  para chains, `\` para line continuation. En Windows: la unica terminal
  soportada es WSL Ubuntu (NO Git Bash, NO PowerShell). Esta eleccion
  vale de §0.3 hasta el final de la guia.
- **Task**: las operaciones AWS se invocan como `task <namespace>:<accion>`
  (e.g. `task infra:apply`, `task batch:retrain VARIETIES=POP`). El
  binario de Task se instala **dentro de WSL** (ver §0.3.3); aunque Task
  tambien corre nativo en Windows, mezclar ambos host filesystems con la
  misma carpeta `infra/` da problemas de permisos y line endings.
- **Comandos de un solo shot**: si ves un bloque con `# UNA SOLA VEZ`,
  es una operacion irreversible (bootstrap, OIDC provider, primera
  creacion de algo). Releelo antes de pegarlo.
- **PowerShell / Git Bash**: la guia NO esta optimizada para PowerShell
  ni Git Bash. Usar siempre WSL Ubuntu en Windows
  (memoria `feedback_shell_bash.md`).

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
Parte 0.3 (prereqs validados)
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

```bash
#!/usr/bin/env bash
# infra/bootstrap.sh — Bootstrap del backend Terraform.
# UNA VEZ por cuenta + region. Idempotente.

set -euo pipefail

PROJECT="${PROJECT:-ml-training}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
# Mismas convenciones que §0.4 (ACCOUNT_ID / ACCOUNT_SUFFIX) — si el
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

Salida esperada (resumida):

```
==> Bootstrap config:
    PROJECT  = ml-training
    REGION   = us-east-1
    ACCOUNT  = 123456789012 (suffix=789012)
    BUCKET   = ml-training-tfstate-789012
    LOCK_TBL = ml-training-tflock

==> [1/4] Creando S3 bucket ml-training-tfstate-789012...
==> [2/4] Activando versioning...
==> [3/4] Activando encryption SSE-S3...
==> [4/4] Creando DynamoDB table ml-training-tflock...
==> [5/6] Asegurando SLRs...

==> BOOTSTRAP COMPLETADO
```

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
│   │   ├── network/                        # 3.3
│   │   ├── storage/                        # 3.4
│   │   ├── mlflow/                         # 3.5
│   │   ├── reports/                        # 3.6
│   │   ├── batch/                          # 3.7
│   │   ├── monitoring/                     # 3.8
│   │   ├── lambdas/                        # 3.9
│   │   ├── scheduler/                      # 3.10
│   │   └── cicd/                           # 3.11
│   └── lambdas/                            # Codigo Python de las Lambdas
│       ├── dispatcher.py                   # 3.9.5
│       ├── notifier.py                     # 3.9.6
│       └── scheduler.py                    # 3.10.5
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
    "infra/modules/network"
    "infra/modules/storage"
    "infra/modules/mlflow"
    "infra/modules/reports"
    "infra/modules/batch"
    "infra/modules/monitoring"
    "infra/modules/lambdas"
    "infra/modules/scheduler"
    "infra/modules/cicd"
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
  description = "Variedades validas. Tiene que matchear src/orchestration/cli.py:resolve_varieties."
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

### 3.2.5 `infra/envs/prod/main.tf`

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

# -------------------------------------------------------------------------
# Capa 1: Red (VPC + subnets + NAT + SGs)
# -------------------------------------------------------------------------
module "network" {
  source   = "../../modules/network"
  project  = var.project
  vpc_cidr = var.vpc_cidr
}

# -------------------------------------------------------------------------
# Capa 2: Storage (S3 buckets + ECR repos)
# -------------------------------------------------------------------------
module "storage" {
  source  = "../../modules/storage"
  project = var.project
}

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

# -------------------------------------------------------------------------
# Capa 7: Lambdas (dispatcher + notifier)
# -------------------------------------------------------------------------
module "lambdas" {
  source = "../../modules/lambdas"

  project                = var.project
  job_queue_spot_arn     = module.batch.job_queue_spot_arn
  job_queue_ondemand_arn = module.batch.job_queue_ondemand_arn
  job_definition_name    = module.batch.job_definition_name
  data_bucket            = module.storage.data_bucket
  varieties_allowed      = var.varieties_allowed
  sns_topic_arn          = module.monitoring.sns_topic_arn
  log_retention_days     = var.log_retention_days
  lambdas_src_dir        = "${path.module}/../../lambdas"
}

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
  work_start_hour_local    = var.work_start_hour_local
  work_end_hour_local      = var.work_end_hour_local
  log_retention_days       = var.log_retention_days
  lambdas_src_dir          = "${path.module}/../../lambdas"
}

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
```

## 3.3 `modules/network/` — VPC + subnets + NAT + SGs

Single-AZ a proposito (Sec 0.2 lockeada). El SG matrix es:

- `sg_alb`: ingress :80 from 0.0.0.0/0 (futuro: WAF + TLS en Parte 10.1)
- `sg_mlflow`: ingress :5000 from `sg_alb` solo
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

## 3.4 `modules/storage/` — S3 buckets + ECR repos

### 3.4.1 `modules/storage/variables.tf`

```hcl
variable "project" { type = string }
```

### 3.4.2 `modules/storage/main.tf`

```hcl
data "aws_caller_identity" "current" {}

locals {
  # Mismo sufijo que calcula bash en bootstrap.sh con `${ACCOUNT: -6}`.
  # Para un account_id estandar de 12 digitos, substr(...,6,6) toma los
  # caracteres en posiciones 6-11 (indices 0-based), que son los ULTIMOS
  # 6 caracteres. Equivalencia con bash: `${ACCOUNT: -6}` → tail 6 chars.
  # Esto asegura que el bucket de tfstate (creado a mano por bootstrap.sh)
  # y los buckets data/artifacts (creados por Terraform) compartan el
  # mismo sufijo, evitando confusion operativa.
  account_suffix = substr(data.aws_caller_identity.current.account_id, 6, 6)
}

# ----- S3: data (input Excels) -----------------------------------------
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

# ----- S3: artifacts (modelos + reportes; MLflow artifact store) ------
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

# Lifecycle: borrar versiones no-current despues de 90 dias.
# Por que 90 (y no 30 ni 365): 3 meses cubre un ciclo razonable de
# A/B testing entre modelos (cuanto tiempo querrias mirar atras para
# comparar un campeon contra su predecesor). Mas corto perderia
# auditoria de incidentes pasados (e.g., "el modelo de hace 2 meses
# que se rompio en prod"); mas largo infla el bill S3 sin valor
# operativo (los artifacts viejos se vuelven "data fria" sin uso).
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

# ----- ECR: trainer ----------------------------------------------------
resource "aws_ecr_repository" "trainer" {
  name                 = var.project
  image_tag_mutability = "MUTABLE" # CI/CD reusa tag "latest" + sha
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

# Lifecycle: mantener ultimas 10 tags + borrar untagged > 7 dias.
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

# ----- ECR: MLflow custom ----------------------------------------------
resource "aws_ecr_repository" "mlflow" {
  name                 = "${var.project}-mlflow"
  image_tag_mutability = "IMMUTABLE" # v3.12.0 nunca cambia
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

# ----- ECR: reports (nginx + s3-sync) ----------------------------------
resource "aws_ecr_repository" "reports" {
  name                 = "${var.project}-reports"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}
```

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

```hcl
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "mlflow_exec" {
  name               = "${var.project}-mlflow-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
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

# IAM: task role (S3 artifacts read/write)
resource "aws_iam_role" "mlflow_task" {
  name               = "${var.project}-mlflow-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
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

#### 3.5.2.e — Log group + Task Definition + Service

El task-def encapsula la receta del container (imagen, comando,
healthcheck, secrets). El service mantiene N replicas corriendo
(`desiredCount=1`) y se conecta al ALB target group. `ignore_changes
= [desired_count]` permite al scheduler bajar a 0 sin que el siguiente
`terraform apply` lo vuelva a subir.

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

# IAM
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "reports_exec" {
  name               = "${var.project}-reports-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "reports_exec" {
  role       = aws_iam_role.reports_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "reports_task" {
  name               = "${var.project}-reports-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
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

```hcl
# Role asumido por la EC2 que lanza Batch (instance profile)
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "batch_instance" {
  name               = "${var.project}-batch-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
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
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "job" {
  name               = "${var.project}-job-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
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
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "exec" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Service role de Batch (gestion de CE)
resource "aws_iam_role" "batch_service" {
  name = "${var.project}-batch-service"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "batch.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
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

```hcl
resource "aws_batch_compute_environment" "spot" {
  compute_environment_name = "${var.project}-ce-spot"
  service_role             = aws_iam_role.batch_service.arn
  type                     = "MANAGED"
  state                    = "ENABLED"

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
  compute_environment_name = "${var.project}-ce-ondemand"
  service_role             = aws_iam_role.batch_service.arn
  type                     = "MANAGED"
  state                    = "ENABLED"

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
variable "job_definition_name" { type = string }
variable "data_bucket" { type = string }
variable "varieties_allowed" { type = list(string) }
variable "sns_topic_arn" { type = string }
variable "log_retention_days" { type = number }
variable "lambdas_src_dir" { type = string }
```

### 3.9.2 `modules/lambdas/dispatcher.tf`

```hcl
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Empaca el codigo Python en zip
data "archive_file" "dispatcher" {
  type        = "zip"
  source_file = "${var.lambdas_src_dir}/dispatcher.py"
  output_path = "${path.module}/dispatcher.zip"
}

# IAM
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dispatcher" {
  name               = "${var.project}-dispatcher"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
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
      # AWS Batch SubmitJob/ListJobs aceptan ARN o name; usamos NAME
      # para mantener consistencia con scheduler/main.tf (que tambien
      # pasa name). Si se quisiera pinear a un ARN historico (rare),
      # cambiar a var.job_queue_spot_arn (ambas son output de batch/).
      JOB_QUEUE_SPOT     = "${var.project}-job-queue-spot"
      JOB_QUEUE_ONDEMAND = "${var.project}-job-queue-ondemand"
      JOB_DEFINITION     = var.job_definition_name
      DATA_BUCKET        = var.data_bucket
      VARIETIES_ALLOWED  = join(",", var.varieties_allowed)
    }
  }

  depends_on = [aws_cloudwatch_log_group.dispatcher]
}
```

### 3.9.3 `modules/lambdas/notifier.tf`

```hcl
data "archive_file" "notifier" {
  type        = "zip"
  source_file = "${var.lambdas_src_dir}/notifier.py"
  output_path = "${path.module}/notifier.zip"
}

resource "aws_iam_role" "notifier" {
  name               = "${var.project}-notifier"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
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
      SNS_TOPIC_ARN = var.sns_topic_arn
      PROJECT       = var.project # usado para construir el URL de CloudWatch
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
# AWS_REGION lo inyecta Lambda runtime automaticamente; PROJECT lo
# pasa el .tf para que el URL del log no hardcodee "ml-training".
AWS_REGION    = os.environ.get("AWS_REGION", "us-east-1")
PROJECT       = os.environ.get("PROJECT", "ml-training")


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
        # URL de CloudWatch logs en consola. $252F = "/" URL-encoded x2
        # (CloudWatch UI hace doble-decode del log group name).
        log_group_encoded = f"$252Faws$252Fbatch$252F{PROJECT}"
        log_url = (
            f"https://{AWS_REGION}.console.aws.amazon.com/cloudwatch/home"
            f"?region={AWS_REGION}#logsV2:log-groups/log-group/"
            f"{log_group_encoded}/log-events/"
            f"{log_stream.replace('/', '$252F')}"
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
variable "work_start_hour_local" { type = number }
variable "work_end_hour_local" { type = number }
variable "tz_offset_hours" {
  type    = number
  default = -5 # PET (Peru)
}
variable "workdays_cron" {
  type    = string
  default = "MON-FRI"
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

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
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
  timeout          = 300
  memory_size      = 256

  environment {
    variables = {
      PROJECT            = var.project
      ECS_CLUSTER        = var.ecs_cluster_name
      ECS_SVC_MLFLOW     = var.ecs_service_name_mlflow
      ECS_SVC_REPORTS    = var.ecs_service_name_reports
      RDS_INSTANCE       = var.rds_instance_id
      JOB_QUEUE_SPOT     = "${var.project}-job-queue-spot"
      JOB_QUEUE_ONDEMAND = "${var.project}-job-queue-ondemand"
    }
  }

  depends_on = [aws_cloudwatch_log_group.scheduler]
}
```

#### 3.10.2.b — EventBridge rules (start, stop, keepstop)

3 crons: `start` (8 AM PET), `stop` (12 PM PET), `keepstop` (cada 6h
defensa contra el auto-arranque de RDS post-7-dias-stopped). El offset
PET→UTC se calcula en `locals` y se enchufa al `cron(0 H ? * MON-FRI *)`.

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

```hcl
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  repo_subject = "repo:${var.github_org}/${var.github_repo}:*"
}

# ----- Role 1: gha-deploy (CI workflows que aplican terraform + push ECR)
data "aws_iam_policy_document" "deploy_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.repo_subject]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name               = "${var.project}-gha-deploy"
  assume_role_policy = data.aws_iam_policy_document.deploy_assume.json
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
  assume_role_policy = data.aws_iam_policy_document.deploy_assume.json
}

resource "aws_iam_role_policy" "train" {
  role = aws_iam_role.train.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project}-dispatcher"
      },
      {
        Effect   = "Allow"
        Action   = ["batch:DescribeJobs", "batch:ListJobs"]
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

> **Fin de la oleada 2 (Parte 3 — modulos Terraform).**
>
> Estado actual: el repo tiene escritos `infra/envs/prod/` (6 archivos)
> + 9 modulos en `infra/modules/` + 3 archivos Python en `infra/lambdas/`
> + 3 archivos en `docker/reports/`. Todo lockeado al contrato real del
> trainer (env vars S3, command `--varieties X --tuning Y`, variedades
> validas, custom metric MAPE dimension `variety`).
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

Si falta (ya cubierto en §0.3.3; recordatorio aqui):

```bash
# Windows (WSL Ubuntu) y Linux: mismo instalador
sh -c "$(curl --location https://taskfile.dev/install.sh)" -- -d -b ~/bin
export PATH="$HOME/bin:$PATH"   # agregar a ~/.bashrc para persistir

# macOS
brew install go-task
```

### 4.1.2 Estructura final

Despues de seguir §4.1.3 a §4.1.9, tu proyecto va a tener:

```
Taskfile.yml                    # raiz: tasks LOCALES (Docker) + includes AWS
tasks/
├── infra.yml                   # infra:*       terraform + bootstrap
├── ecr.yml                     # ecr:*         build + push 3 imagenes
├── batch.yml                   # batch:*       submit jobs + polling
├── cluster.yml                 # cluster:*     lifecycle scale up/down + teardown
├── mlflow_registry.yml         # mlflow-aws:*  promote con quality gate MAPE
└── aws.yml                     # aws:*         orquestadores high-level (deploy/wake/sleep)
```

**Por que un Taskfile raiz + 6 archivos en `tasks/` y no uno solo**:

- **Namespacing**: cada `includes:` prefija con `<nombre>:`, asi `task build`
  (local Docker existente) NO choca con `task ecr:build` (AWS nuevo).
- **Blast radius**: tocar `tasks/batch.yml` no riesga romper `cluster.yml`.
- **Discoverability**: `task --list` los muestra agrupados por namespace.
- **Tamano manageable**: un Taskfile monolitico de 600 lineas se vuelve un
  dolor de revisar; 6 archivos de 80-120 lineas son trozos auto-contenidos.

### 4.1.3 Crear `tasks/` y anadir `PROJECT` al Taskfile raiz

> **Orden importa**: el `includes:` viene en §4.1.10 — DESPUES de crear
> los 6 archivos referenciados (§4.1.4 a §4.1.9). Si pegas el
> `includes:` ahora, `task --list` falla por "archivo no encontrado"
> hasta que llegues a 4.1.10.

```bash
mkdir -p tasks
```

Editar `Taskfile.yml` raiz para anadir `PROJECT` al bloque `vars:`
existente (esto SI se hace ahora — los Taskfiles que vas a crear en
§4.1.4-4.1.9 lo van a leer):

```yaml
vars:
  # ... vars existentes (TUNING, VARIETIES, PARALLEL) ...
  PROJECT: "{{.PROJECT | default \"ml-training\"}}"   # NUEVO, usado por tasks AWS
```

**Por que `PROJECT` se agrega al root**: las tasks AWS lo usan como nombre
base para todos los recursos (ECR repos, RDS instance, Batch queues, Lambda
function names). Definirlo una sola vez en el root permite override via CLI
(`task aws:deploy PROJECT=ml-training-staging`) sin tocar ningun archivo.

### 4.1.4 `tasks/infra.yml` (Terraform wrapper + bootstrap)

Crear el archivo con este contenido:

```yaml
# =============================================================================
# tasks/infra.yml  -  Terraform + bootstrap del backend
# =============================================================================
# Incluido por Taskfile.yml raiz con namespace "infra:".

version: "3"

vars:
  TF_DIR: '{{.TF_DIR | default "infra/envs/prod"}}'

tasks:

  # ----- Bootstrap (one-shot) ------------------------------------------------

  bootstrap:
    desc: "Bootstrap backend Terraform (S3 + DynamoDB lock + SLRs). UNA VEZ por cuenta+region. Idempotente"
    cmds:
      - bash infra/bootstrap.sh

  bootstrap-oidc:
    desc: "Crear rol IAM para GitHub Actions via OIDC. UNA VEZ. Idempotente"
    cmds:
      - bash infra/bootstrap-oidc.sh

  # ----- Init (interno, deps de plan/apply/destroy) --------------------------

  _init:
    internal: true
    # Resolvemos ACCOUNT_SUFFIX en cada init: cambia entre cuentas (sandbox vs
    # prod) y queremos el backend correcto sin obligar a setear ACCOUNT_SUFFIX
    # en .env. `tail -c 7` toma los ultimos 6 chars del Account ID + newline.
    vars:
      SUFFIX:
        sh: aws sts get-caller-identity --query Account --output text | tail -c 7
      PROJECT: '{{.PROJECT | default "ml-training"}}'
      REGION: '{{.AWS_DEFAULT_REGION | default "us-east-1"}}'
    cmds:
      - terraform -chdir={{.TF_DIR}} init
        -backend-config=bucket={{.PROJECT}}-tfstate-{{.SUFFIX}}
        -backend-config=key=envs/prod/terraform.tfstate
        -backend-config=region={{.REGION}}
        -backend-config=dynamodb_table={{.PROJECT}}-tflock
        -reconfigure

  # ----- Plan / apply / destroy ----------------------------------------------

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
    desc: "DESTRUCTIVO: terraform destroy completo. Pide confirmacion. Considerar `task cluster:teardown` antes (preserva storage)"
    prompt: "Esto borrara TODA la infra de envs/prod, incluso storage (S3 buckets, ECR repos). Continuar?"
    deps: [_init]
    cmds:
      - terraform -chdir={{.TF_DIR}} destroy -auto-approve

  destroy-target:
    desc: "terraform destroy parcial. Vars: TARGET=module.X (REQUERIDO). Pide confirmacion"
    prompt: "Destruir {{.TARGET}}? Asegurate que no tenga dependencias activas"
    deps: [_init]
    cmds:
      - 'test -n "{{.TARGET}}" || { echo "ERROR falta TARGET=module.X"; exit 1; }'
      - terraform -chdir={{.TF_DIR}} destroy -target={{.TARGET}} -auto-approve

  # ----- Inspeccion ----------------------------------------------------------

  output:
    desc: "Mostrar outputs de envs/prod (alb_dns, ecr_urls, rds_endpoint, ...)"
    cmds:
      - terraform -chdir={{.TF_DIR}} output

  output-raw:
    desc: "Mostrar UN output crudo (para scripts). Var: NAME=alb_dns"
    silent: true
    cmds:
      - 'test -n "{{.NAME}}" || { echo "ERROR falta NAME=<output>"; exit 1; }'
      - terraform -chdir={{.TF_DIR}} output -raw {{.NAME}}

  validate:
    desc: "terraform fmt -check + validate. Sin tocar state. Util en pre-commit"
    cmds:
      - terraform -chdir={{.TF_DIR}} fmt -check -recursive
      - terraform -chdir={{.TF_DIR}} validate

  # ----- Recovery ------------------------------------------------------------

  force-unlock:
    desc: "Liberar state lock huerfano. Var: LOCK_ID=<id que muestra el error>"
    deps: [_init]
    cmds:
      - 'test -n "{{.LOCK_ID}}" || { echo "ERROR falta LOCK_ID=<id>"; exit 1; }'
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

version: "3"

vars:
  PROJECT: '{{.PROJECT | default "ml-training"}}'
  REGION: '{{.AWS_DEFAULT_REGION | default "us-east-1"}}'

  # Tags default (override por CLI: task ecr:build IMG=trainer TAG=v1.2.3)
  TAG_TRAINER: '{{.TAG_TRAINER | default "latest"}}'
  TAG_MLFLOW: '{{.TAG_MLFLOW | default "v3.12.0"}}'
  TAG_REPORTS: '{{.TAG_REPORTS | default "stable"}}'

tasks:

  # ----- Login (12h validez del token) ---------------------------------------

  login:
    desc: "docker login a ECR (token valido 12h). Idempotente"
    # run: once: si varias tasks dependen de login en una misma corrida,
    # solo se ejecuta una vez.
    run: once
    vars:
      ACCOUNT:
        sh: aws sts get-caller-identity --query Account --output text
    cmds:
      - aws ecr get-login-password --region {{.REGION}}
        | docker login --username AWS --password-stdin {{.ACCOUNT}}.dkr.ecr.{{.REGION}}.amazonaws.com

  # ----- Build + push UNA imagen (parametrizado) -----------------------------

  build:
    desc: "Build + push UNA imagen. Vars: IMG=trainer|mlflow|reports (REQUERIDO), TAG=<override opcional>"
    deps: [login]
    vars:
      ACCOUNT:
        sh: aws sts get-caller-identity --query Account --output text
      REGISTRY: '{{.ACCOUNT}}.dkr.ecr.{{.REGION}}.amazonaws.com'
      GIT_SHA:
        sh: git rev-parse --short=12 HEAD 2>/dev/null || echo unknown
      BUILD_DATE:
        sh: date -u +%Y-%m-%dT%H:%M:%SZ
      # Resolvemos image name, dockerfile y tag segun IMG
      IMAGE_NAME:
        sh: |
          case "{{.IMG}}" in
            trainer) echo "{{.PROJECT}}" ;;
            mlflow)  echo "{{.PROJECT}}-mlflow" ;;
            reports) echo "{{.PROJECT}}-reports" ;;
            *) echo "ERROR_IMG_DESCONOCIDO" ;;
          esac
      DOCKERFILE:
        sh: |
          case "{{.IMG}}" in
            trainer) echo "Dockerfile" ;;
            mlflow)  echo "docker/mlflow/Dockerfile" ;;
            reports) echo "docker/reports/Dockerfile" ;;
            *) echo "INVALID" ;;
          esac
      RESOLVED_TAG:
        sh: |
          case "{{.IMG}}" in
            trainer) echo "{{.TAG | default .TAG_TRAINER}}" ;;
            mlflow)  echo "{{.TAG | default .TAG_MLFLOW}}" ;;
            reports) echo "{{.TAG | default .TAG_REPORTS}}" ;;
            *) echo "INVALID" ;;
          esac
    cmds:
      - 'test "{{.IMAGE_NAME}}" != "ERROR_IMG_DESCONOCIDO" || { echo "ERROR IMG debe ser trainer/mlflow/reports (recibido {{.IMG}})"; exit 1; }'
      - 'echo ">>> Build {{.IMAGE_NAME}}:{{.RESOLVED_TAG}} (sha-{{.GIT_SHA}})"'
      - docker build
        --build-arg GIT_SHA={{.GIT_SHA}}
        --build-arg BUILD_DATE={{.BUILD_DATE}}
        --build-arg VERSION={{.RESOLVED_TAG}}
        -t {{.REGISTRY}}/{{.IMAGE_NAME}}:{{.RESOLVED_TAG}}
        -t {{.REGISTRY}}/{{.IMAGE_NAME}}:sha-{{.GIT_SHA}}
        -f {{.DOCKERFILE}} .
      - docker push {{.REGISTRY}}/{{.IMAGE_NAME}}:{{.RESOLVED_TAG}}
      - docker push {{.REGISTRY}}/{{.IMAGE_NAME}}:sha-{{.GIT_SHA}}

  # ----- Build + push de las 3 ------------------------------------------------

  build-all:
    desc: "Build + push de las 3 imagenes (trainer + mlflow + reports) con tags default"
    deps: [login]
    cmds:
      - task: build
        vars: { IMG: trainer }
      - task: build
        vars: { IMG: mlflow }
      - task: build
        vars: { IMG: reports }

  # ----- Inspeccion -----------------------------------------------------------

  list:
    desc: "Listar las 3 imagenes con tag default presente en cada repo ECR"
    silent: true
    cmds:
      - 'echo "=== {{.PROJECT}} ({{.TAG_TRAINER}}) ==="'
      - aws ecr list-images --repository-name {{.PROJECT}} --query 'imageIds[?imageTag==`{{.TAG_TRAINER}}`]' --output table || true
      - 'echo ""'
      - 'echo "=== {{.PROJECT}}-mlflow ({{.TAG_MLFLOW}}) ==="'
      - aws ecr list-images --repository-name {{.PROJECT}}-mlflow --query 'imageIds[?imageTag==`{{.TAG_MLFLOW}}`]' --output table || true
      - 'echo ""'
      - 'echo "=== {{.PROJECT}}-reports ({{.TAG_REPORTS}}) ==="'
      - aws ecr list-images --repository-name {{.PROJECT}}-reports --query 'imageIds[?imageTag==`{{.TAG_REPORTS}}`]' --output table || true
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

**Por que `case` con `sh:` para resolver IMG -> image_name/dockerfile/tag**:
en Task, `vars` con `sh:` se ejecutan al cargar la task (no en cada
comando). Permite parametrizar sin escribir 3 tasks separadas
(`build:trainer`, `build:mlflow`, `build:reports`). La validacion `test
... ERROR_IMG_DESCONOCIDO` falla rapido si IMG mal escrito.

**Por que pasamos `BUILD_DATE` como build-arg**: el Dockerfile lo recibe
y lo embebe como label (`org.opencontainers.image.created`). Util para
auditar "que imagen tengo desplegada y desde cuando" via
`docker inspect <image>`.

### 4.1.6 `tasks/batch.yml` (submit jobs + polling)

```yaml
# =============================================================================
# tasks/batch.yml  -  Submitir jobs a AWS Batch + polling de status
# =============================================================================
# Incluido por Taskfile.yml raiz con namespace "batch:".

version: "3"

vars:
  PROJECT: '{{.PROJECT | default "ml-training"}}'
  JOB_DEF: '{{.JOB_DEF | default (printf "%s-trainer" (.PROJECT | default "ml-training"))}}'
  QUEUE_SPOT: '{{.QUEUE_SPOT | default (printf "%s-spot" (.PROJECT | default "ml-training"))}}'
  QUEUE_OD: '{{.QUEUE_OD | default (printf "%s-ondemand" (.PROJECT | default "ml-training"))}}'

  TUNING: '{{.TUNING | default "prod"}}'
  PARALLEL: '{{.PARALLEL | default "1"}}'
  WAIT: '{{.WAIT | default "true"}}'

tasks:

  # ----- Submit + (opcional) wait ---------------------------------------------

  submit:
    desc: "Lanza UN job a Batch. Vars: VARIETY=POP (REQUERIDO), TUNING, WAIT=true|false"
    vars:
      QUEUE: '{{if eq .TUNING "prod_xl"}}{{.QUEUE_OD}}{{else}}{{.QUEUE_SPOT}}{{end}}'
      JOB_NAME: 'train-{{.VARIETY}}-{{.TUNING}}-{{now | date "20060102-150405"}}'
    cmds:
      - 'test -n "{{.VARIETY}}" || { echo "ERROR falta VARIETY=<nombre>"; exit 1; }'
      - |
        JOB_ID=$(aws batch submit-job \
          --job-name "{{.JOB_NAME}}" \
          --job-queue "{{.QUEUE}}" \
          --job-definition "{{.JOB_DEF}}" \
          --container-overrides '{
            "command": [
              "--varieties", "{{.VARIETY}}",
              "--tuning", "{{.TUNING}}",
              "--parallel-varieties", "{{.PARALLEL}}"
            ]
          }' \
          --query 'jobId' --output text)
        echo "Submitted JOB_ID=$JOB_ID  (queue={{.QUEUE}}, def={{.JOB_DEF}})"
        if [ "{{.WAIT}}" = "true" ]; then
          task batch:wait JOB_ID=$JOB_ID
        fi

  # ----- Wait sobre un JOB_ID -------------------------------------------------

  wait:
    desc: "Polling de un job hasta SUCCEEDED/FAILED. Var: JOB_ID=<id>. Sale con exit 1 si FAILED"
    cmds:
      - 'test -n "{{.JOB_ID}}" || { echo "ERROR falta JOB_ID=<id>"; exit 1; }'
      - |
        echo "Polling job {{.JOB_ID}} cada 30s..."
        while true; do
          STATUS=$(aws batch describe-jobs --jobs "{{.JOB_ID}}" --query 'jobs[0].status' --output text)
          REASON=$(aws batch describe-jobs --jobs "{{.JOB_ID}}" --query 'jobs[0].statusReason' --output text 2>/dev/null || echo "-")
          echo "  $(date +%H:%M:%S)  status=$STATUS  reason=$REASON"
          case "$STATUS" in
            SUCCEEDED) echo "OK job {{.JOB_ID}} SUCCEEDED"; exit 0 ;;
            FAILED)    echo "FAIL job {{.JOB_ID}} FAILED $REASON"; exit 1 ;;
            SUBMITTED|PENDING|RUNNABLE|STARTING|RUNNING) sleep 30 ;;
            *)         echo "Estado desconocido $STATUS"; exit 2 ;;
          esac
        done

  # ----- Smoke test -----------------------------------------------------------

  smoke:
    desc: "Smoke test lanza UN job con VARIETY=POP TUNING=smoke (~1 min). Falla con exit 1 si el job falla"
    cmds:
      - task: submit
        vars: { VARIETY: POP, TUNING: smoke, WAIT: "true" }

  # ----- Retrain (multi-variedad, secuencial) --------------------------------

  retrain:
    desc: "Lanza N jobs (1 por variedad). Vars VARIETIES=POP,JUPITER (REQUERIDO), TUNING, WAIT"
    cmds:
      - 'test -n "{{.VARIETIES}}" || { echo "ERROR falta VARIETIES=POP[,JUPITER,...]"; exit 1; }'
      - |
        # Submit en serie. Si WAIT=true, espera entre uno y otro (orden
        # determinista). Si WAIT=false, dispara todos y vuelve.
        for v in $(echo "{{.VARIETIES}}" | tr ',' ' '); do
          echo ">>> Lanzando retrain de variedad=$v tuning={{.TUNING}}"
          task batch:submit VARIETY=$v TUNING={{.TUNING}} WAIT={{.WAIT}} || {
            echo "FAIL variedad $v fallo. Abortando resto."
            exit 1
          }
        done

  # ----- Estado --------------------------------------------------------------

  status:
    desc: "Jobs no terminados (SUBMITTED/PENDING/RUNNABLE/STARTING/RUNNING) en ambas queues"
    silent: true
    cmds:
      - 'echo "=== Queue Spot ({{.QUEUE_SPOT}}) ==="'
      - |
        for s in SUBMITTED PENDING RUNNABLE STARTING RUNNING; do
          aws batch list-jobs --job-queue "{{.QUEUE_SPOT}}" --job-status $s \
            --query 'jobSummaryList[].[jobId,jobName,status,createdAt]' --output table 2>/dev/null || true
        done
      - 'echo ""'
      - 'echo "=== Queue OnDemand ({{.QUEUE_OD}}) ==="'
      - |
        for s in SUBMITTED PENDING RUNNABLE STARTING RUNNING; do
          aws batch list-jobs --job-queue "{{.QUEUE_OD}}" --job-status $s \
            --query 'jobSummaryList[].[jobId,jobName,status,createdAt]' --output table 2>/dev/null || true
        done

  cancel:
    desc: "Cancelar un job RUNNING o PENDING. Var JOB_ID=<id> REASON=<texto>"
    cmds:
      - 'test -n "{{.JOB_ID}}" || { echo "ERROR falta JOB_ID=<id>"; exit 1; }'
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

**Por que `submit` invoca `batch:wait` como sub-task y no inline el
polling**: separa concerns (submit = "lanzar", wait = "esperar") y
permite usar `wait` solo (`task batch:wait JOB_ID=<id>`) si ya tenes
un job lanzado por otra via.

**Por que `retrain` itera con bash `for` y no con paralelismo Task**:
los jobs Batch ya corren en paralelo en infraestructura AWS. El loop
local solo necesita lanzarlos secuencialmente para que el polling sea
ordenado en la terminal. Si querias todo paralelo, `WAIT=false`.

### 4.1.7 `tasks/cluster.yml` (lifecycle scale up/down + teardown)

```yaml
# =============================================================================
# tasks/cluster.yml  -  Lifecycle del cluster AWS (scale up/down + teardown)
# =============================================================================
# Incluido por Taskfile.yml raiz con namespace "cluster:".
#
# Modulos "volatiles" (se reconstruyen en ~10-15 min):
#   scheduler, lambdas, monitoring, batch, reports, mlflow
# Modulos "permanentes" (NO se tocan en teardown):
#   network (VPC + NAT $$$), storage (S3 + ECR), backend state

version: "3"

vars:
  PROJECT: '{{.PROJECT | default "ml-training"}}'
  DISPATCHER_FN: '{{.DISPATCHER_FN | default (printf "%s-dispatcher" (.PROJECT | default "ml-training"))}}'
  TF_DIR: '{{.TF_DIR | default "infra/envs/prod"}}'

  # Modulos volatiles (orden importa para destroy: reverse de apply)
  VOLATILE_MODULES: "module.scheduler module.lambdas module.monitoring module.batch module.reports module.mlflow"

tasks:

  # ----- Estado actual --------------------------------------------------------

  status:
    desc: "Estado actual del cluster RDS + ECS services + Batch jobs activos"
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
        for q in {{.PROJECT}}-spot {{.PROJECT}}-ondemand; do
          for s in SUBMITTED PENDING RUNNABLE STARTING RUNNING; do
            n=$(aws batch list-jobs --job-queue "$q" --job-status $s --query 'length(jobSummaryList)' --output text 2>/dev/null || echo 0)
            if [ "$n" -gt 0 ]; then
              echo "  queue=$q  status=$s  count=$n"
              total=$((total + n))
            fi
          done
        done
        echo "  TOTAL activos $total"

  # ----- Scale down (apagar) --------------------------------------------------

  scale-down:
    desc: "Apaga RDS + ECS services (desired=0). Aborta si hay Batch jobs RUNNING. Invoca Lambda dispatcher"
    cmds:
      - 'echo ">>> Pre-check Batch jobs activos"'
      - task: _batch-jobs-active
      - |
        # Chequeo previo para fallar antes de tocar nada si hay jobs corriendo.
        # El dispatcher tambien chequea, pero lo hacemos visible aqui.
        running=$(aws batch list-jobs --job-queue "{{.PROJECT}}-spot" --job-status RUNNING --query 'length(jobSummaryList)' --output text 2>/dev/null || echo 0)
        if [ "$running" -gt 0 ]; then
          echo "ERROR $running job(s) RUNNING en queue spot. Esperar o cancelar antes de scale-down."
          echo "       task batch:status   para ver detalle"
          echo "       task batch:cancel JOB_ID=<id>   para cancelar"
          exit 1
        fi
      - 'echo ">>> Invocando dispatcher Lambda (action=stop)..."'
      - aws lambda invoke --function-name {{.DISPATCHER_FN}}
        --payload '{"action":"stop"}'
        --cli-binary-format raw-in-base64-out
        /tmp/dispatcher-stop.json
      - cat /tmp/dispatcher-stop.json && echo ""

  # ----- Scale up (encender) --------------------------------------------------

  scale-up:
    desc: "Arranca RDS + ECS services (desired=1). Invoca Lambda dispatcher. RDS tarda ~5 min en estar disponible"
    cmds:
      - 'echo ">>> Invocando dispatcher Lambda (action=start)..."'
      - aws lambda invoke --function-name {{.DISPATCHER_FN}}
        --payload '{"action":"start"}'
        --cli-binary-format raw-in-base64-out
        /tmp/dispatcher-start.json
      - cat /tmp/dispatcher-start.json && echo ""
      - 'echo ""'
      - 'echo "RDS tarda ~5 min en estar disponible. Despues task cluster:wait-healthy"'

  # ----- Wait healthy ---------------------------------------------------------

  wait-healthy:
    desc: "Polling del ALB hasta que MLflow responda 200. Timeout 10 min"
    deps: [_init-alb]
    cmds:
      - |
        # ALB resuelto via output de Terraform (terraform output -raw alb_dns)
        ALB=$(terraform -chdir={{.TF_DIR}} output -raw alb_dns 2>/dev/null)
        if [ -z "$ALB" ]; then
          echo "ERROR no se pudo leer terraform output alb_dns. Esta envs/prod aplicada?"
          exit 1
        fi
        echo "Polling http://$ALB/ cada 15s (timeout 10 min)..."
        for i in $(seq 1 40); do
          code=$(curl -s -o /dev/null -w '%{http_code}' "http://$ALB/" 2>/dev/null || echo 000)
          echo "  $(date +%H:%M:%S)  GET http://$ALB/  -> $code"
          if [ "$code" = "200" ]; then
            echo "OK MLflow respondiendo en http://$ALB/"
            exit 0
          fi
          sleep 15
        done
        echo "FAIL timeout 10 min esperando ALB. Revisar logs aws logs tail /ecs/{{.PROJECT}}/mlflow --follow"
        exit 1

  _init-alb:
    internal: true
    cmds:
      - 'test -d {{.TF_DIR}} || { echo "ERROR {{.TF_DIR}} no existe. Aplicar infra primero - task infra:apply"; exit 1; }'

  # ----- Teardown / Rebuild ---------------------------------------------------

  teardown:
    desc: "scale-down + destroy de modulos volatiles. Preserva storage + network. Pide confirmacion"
    prompt: "Esto destruira los modulos volatiles. Storage (S3+ECR) y network (VPC) quedan intactos. Continuar?"
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
      - 'echo "OK teardown completo. Para volver task cluster:rebuild"'

  rebuild:
    desc: "Re-apply de modulos volatiles + scale-up. Reconstruye lo que teardown destruyo"
    cmds:
      - 'echo ">>> Apply completo (los modulos volatiles se re-crean)..."'
      - task: ":infra:apply"
      - 'echo ">>> scale-up..."'
      - task: scale-up
      - 'echo ""'
      - 'echo "Listo. task cluster:wait-healthy para confirmar que MLflow responde"'
```

**Por que invocar el Lambda dispatcher y no `aws cli` directo desde la
task**: la logica (drenar Batch -> apagar Fargate -> stop RDS en orden,
mas chequeos y notificaciones SNS) ya vive en `infra/lambdas/dispatcher.py`.
Re-implementarla en bash duplicaria mantenimiento y deriva con el tiempo.

**Por que el pre-check explicito de Batch jobs antes de scale-down (y no
delegado al dispatcher)**: el dispatcher tambien chequea, pero la task
muestra el error en la terminal del operador con sugerencias accionables
(`task batch:cancel JOB_ID=<id>`). Si solo confiaras en el Lambda, el
operador veria un payload JSON con `error: jobs running` y tendria que
buscar como cancelarlos.

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
# tasks/mlflow_registry.yml  -  Promotion en MLflow Model Registry (AWS)
# =============================================================================
# Incluido por Taskfile.yml raiz con namespace "mlflow-aws:".
# Namespace separado del MLflow local (que vive en docker-compose).

version: "3"

vars:
  TF_DIR: '{{.TF_DIR | default "infra/envs/prod"}}'
  MAX_MAPE: '{{.MAX_MAPE | default "20"}}'

tasks:

  # ----- Helper resolver MLflow URI ------------------------------------------

  _mlflow-uri:
    internal: true
    silent: true
    cmds:
      - |
        ALB=$(terraform -chdir={{.TF_DIR}} output -raw alb_dns 2>/dev/null)
        if [ -z "$ALB" ]; then
          echo "ERROR no se pudo leer terraform output alb_dns" >&2
          exit 1
        fi
        echo "http://$ALB"

  # ----- Listar versiones de un modelo ---------------------------------------

  list-versions:
    desc: "Listar versiones de un modelo en el Registry. Var VARIETY=POP (REQUERIDO)"
    cmds:
      - 'test -n "{{.VARIETY}}" || { echo "ERROR falta VARIETY=<nombre>"; exit 1; }'
      - |
        URI=$(task mlflow-aws:_mlflow-uri)
        curl -s "$URI/api/2.0/mlflow/registered-models/get?name={{.VARIETY}}" \
          | jq '.registered_model.latest_versions[] | {version, current_stage, run_id, creation_timestamp}'

  # ----- Promote con quality gate --------------------------------------------

  promote:
    desc: "Promover version a Production con gate MAPE. Vars VARIETY=POP VERSION=N (REQUERIDOS), MAX_MAPE=20"
    cmds:
      - 'test -n "{{.VARIETY}}" || { echo "ERROR falta VARIETY=<nombre>"; exit 1; }'
      - 'test -n "{{.VERSION}}" || { echo "ERROR falta VERSION=<N>"; exit 1; }'
      - |
        URI=$(task mlflow-aws:_mlflow-uri)
        echo ">>> Resolviendo run_id de {{.VARIETY}} v{{.VERSION}}..."
        RUN_ID=$(curl -s "$URI/api/2.0/mlflow/model-versions/get?name={{.VARIETY}}&version={{.VERSION}}" \
          | jq -r '.model_version.run_id')
        if [ -z "$RUN_ID" ] || [ "$RUN_ID" = "null" ]; then
          echo "ERROR no se encontro {{.VARIETY}} v{{.VERSION}} en el Registry"
          exit 1
        fi
        echo "    run_id=$RUN_ID"

        echo ">>> Leyendo metric mape_oof..."
        MAPE=$(curl -s "$URI/api/2.0/mlflow/runs/get?run_id=$RUN_ID" \
          | jq -r '.run.data.metrics[] | select(.key == "mape_oof") | .value')
        if [ -z "$MAPE" ] || [ "$MAPE" = "null" ]; then
          echo "ERROR el run no tiene metric mape_oof. Promote abortado."
          exit 1
        fi
        echo "    mape_oof=$MAPE  (umbral max={{.MAX_MAPE}})"

        # Comparacion float via awk (bash no soporta float nativo).
        OK=$(awk -v m="$MAPE" -v t="{{.MAX_MAPE}}" 'BEGIN { print (m <= t) ? "yes" : "no" }')
        if [ "$OK" != "yes" ]; then
          echo "GATE FAIL MAPE=$MAPE > {{.MAX_MAPE}}. Promote abortado."
          exit 1
        fi
        echo "GATE OK."

        echo ">>> Transicionando a Production (archive existing)..."
        curl -s -X POST "$URI/api/2.0/mlflow/model-versions/transition-stage" \
          -H "Content-Type: application/json" \
          -d "{\"name\":\"{{.VARIETY}}\",\"version\":\"{{.VERSION}}\",\"stage\":\"Production\",\"archive_existing_versions\":true}" \
          | jq '.model_version | {name, version, current_stage}'

        echo "OK {{.VARIETY}} v{{.VERSION}} ahora en Production."

  # ----- Inspeccion de Production actual -------------------------------------

  current-prod:
    desc: "Mostrar la version en Production actual de un modelo. Var VARIETY=POP"
    cmds:
      - 'test -n "{{.VARIETY}}" || { echo "ERROR falta VARIETY=<nombre>"; exit 1; }'
      - |
        URI=$(task mlflow-aws:_mlflow-uri)
        curl -s "$URI/api/2.0/mlflow/registered-models/get?name={{.VARIETY}}" \
          | jq '.registered_model.latest_versions[] | select(.current_stage == "Production") | {version, run_id, creation_timestamp}'
```

**Por que via REST API (`curl + jq`) y no `mlflow` CLI**: el host (Windows
+ WSL Ubuntu, o Linux/Mac) no necesariamente tiene `mlflow` CLI instalado,
y agregar Python + mlflow al host duplica la dependencia que ya vive en
el container del trainer. `curl + jq` son ubicuos y la API REST de MLflow
es estable cross-version (2.x y 3.x).

**Por que `awk` para comparar floats**: bash no soporta comparacion de
floats nativamente (`[ 19.5 -le 20 ]` falla con "integer expression
expected"). `awk` lo hace en una linea.

**Por que el gate chequea `mape_oof` y no `mape`**: `mape_oof` es la
metric out-of-fold (validacion cross-validation), no la del train set.
La metric del train set siempre se ve bien (overfit); `oof` es lo que
predice generalizacion.

**Por que `archive_existing_versions: true` en el POST de
transition-stage**: solo UNA version puede estar "Production" a la vez
por convencion de seguridad (no querer ambiguedad sobre cual modelo
sirve trafico). Archivar las anteriores las saca del set "Production"
sin borrarlas (siguen accesibles via stage="Archived").

### 4.1.9 `tasks/aws.yml` (orquestadores high-level)

```yaml
# =============================================================================
# tasks/aws.yml  -  Orquestadores high-level del stack AWS
# =============================================================================
# Incluido por Taskfile.yml raiz con namespace "aws:".
# Son ATAJOS que encadenan tasks de otros namespaces (infra, ecr, batch,
# cluster) para los flujos completos del runbook.

version: "3"

tasks:

  # ----- Deploy / smoke -------------------------------------------------------

  deploy:
    desc: "Deploy completo apply storage -> build 3 imagenes -> apply resto. Equivalente a oleadas A+B+C"
    cmds:
      - 'echo ">>> Oleada A apply module.storage (S3 + ECR)..."'
      - task: ":infra:apply"
        vars: { TARGET: module.storage }
      - 'echo ">>> Oleada B build + push 3 imagenes..."'
      - task: ":ecr:build-all"
      - 'echo ">>> Oleada C apply resto (network, mlflow, batch, monitoring, ...)..."'
      - task: ":infra:apply"
      - 'echo ""'
      - 'echo "Deploy completo. ALB DNS"'
      - task: ":infra:output-raw"
        vars: { NAME: alb_dns }
      - 'echo ""'

  smoke:
    desc: "Deploy + smoke test. Falla si el smoke job no completa SUCCEEDED"
    cmds:
      - task: deploy
      - 'echo ">>> Smoke test (POP, tuning=smoke, ~1 min)..."'
      - task: ":batch:smoke"

  # ----- Lifecycle (atajos a cluster:) ---------------------------------------

  wake:
    desc: "Encender stack (scale-up + wait-healthy). Para lunes a la manana"
    cmds:
      - task: ":cluster:scale-up"
      - 'echo ""'
      - 'echo "Esperando ~5 min a que RDS este Available antes de probar ALB..."'
      - sleep 300
      - task: ":cluster:wait-healthy"

  sleep:
    desc: "Apagar stack (cluster:scale-down). Para viernes a la noche / fuera de horario"
    cmds:
      - task: ":cluster:scale-down"

  teardown:
    desc: "scale-down + destroy modulos volatiles. Preserva storage + network. Pide confirmacion"
    cmds:
      - task: ":cluster:teardown"

  rebuild:
    desc: "Re-apply de modulos volatiles + scale-up. Reverso del teardown"
    cmds:
      - task: ":cluster:rebuild"

  # ----- Destroy total --------------------------------------------------------

  destroy:
    desc: "DESTRUCTIVO TOTAL terraform destroy de TODO (incluye storage). Pide doble confirmacion"
    prompt: "Esto destruira COMPLETAMENTE envs/prod (S3 buckets, ECR repos, RDS, todo). Es irreversible. Continuar?"
    cmds:
      - 'echo ">>> Doble check drenando Batch jobs primero..."'
      - task: ":cluster:scale-down"
      - 'echo ""'
      - 'echo ">>> terraform destroy total..."'
      - task: ":infra:destroy"

  # ----- Estado --------------------------------------------------------------

  status:
    desc: "Estado completo del stack outputs de Terraform + cluster:status"
    cmds:
      - 'echo "=== Terraform outputs ==="'
      - task: ":infra:output"
      - 'echo ""'
      - 'echo "=== Cluster status ==="'
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

**Por que el `sleep 300` en `aws:wake`**: RDS tarda ~5 min en estar
Available desde "stopped". `cluster:wait-healthy` chequea el ALB cada
15s con timeout de 10 min; sin el sleep previo, los primeros 20 polls
fallarian inutilmente (ALB no puede ser healthy sin backend RDS).

**Por que `aws:destroy` tiene doble confirmacion**: la primera viene de
su propio `prompt:`, la segunda de `infra:destroy` que invoca por
dentro. Es intencional: destruir storage versionado es la operacion mas
peligrosa del runbook, vale la pena un segundo pulse.

### 4.1.10 Anadir `includes:` al Taskfile raiz + verificacion final

Ahora que los 6 `tasks/*.yml` existen (§4.1.4 a §4.1.9), agregar el
bloque `includes:` al `Taskfile.yml` raiz, **despues de `dotenv:` y
antes de `vars:`**:

```yaml
# === ANADIR despues del bloque dotenv existente ===
# Modulos AWS por dominio. Cada include prefija con su namespace, asi las
# tasks locales (build, up, train, ...) no chocan con las AWS (infra:apply,
# ecr:build, ...).
includes:
  infra:
    taskfile: ./tasks/infra.yml
  ecr:
    taskfile: ./tasks/ecr.yml
  batch:
    taskfile: ./tasks/batch.yml
  cluster:
    taskfile: ./tasks/cluster.yml
  mlflow-aws:
    taskfile: ./tasks/mlflow_registry.yml
  aws:
    taskfile: ./tasks/aws.yml
```

Verificacion:

```bash
# Lista plana de todas las tasks. Deberian aparecer namespaces
# infra:*, ecr:*, batch:*, cluster:*, mlflow-aws:*, aws:*
task --list

# Indice guiado del proyecto (local + AWS)
task

# Validar sintaxis de TODOS los Taskfiles sin ejecutar nada
task --list-all > /dev/null && echo "OK"
```

Si `task --list` muestra los 6 namespaces, el setup esta completo. A
partir de aca, las oleadas A/B/C (§4.2 a §4.5) usan estas tasks.

## 4.2 Ola A — apply storage solo

Crea los 2 buckets S3 + 3 repos ECR. Tiempo: ~1 min.

```bash
# Variables de sesion (re-declaradas para que cada oleada sea standalone
# copy-paste-able; si ya las exportaste en §0.4 estas lineas son no-op).
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
en §4.7.1 cuando uses `task batch:retrain`; el smoke va directo a Batch):

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

La logica vive en `tasks/batch.yml` y tiene 2 pasos:

1. **Submit** (`batch:submit VARIETY=POP TUNING=smoke`): invoca `aws batch
   submit-job` con la job-definition `ml-training-trainer` y container
   overrides `--varieties POP --tuning smoke --parallel-varieties 1`.
2. **Wait** (`batch:wait JOB_ID=<id>`): polling cada 30s sobre
   `aws batch describe-jobs` hasta que `status` sea `SUCCEEDED` o
   `FAILED`. Exit 0 / exit 1 respectivamente.

**Por que NO via Lambda dispatcher** (a diferencia del retrain): la
task local hace submit directo a Batch para tener control fino del
exit code y diagnostico. El Lambda dispatcher es para invocaciones
desde cron/EventBridge donde no hay un humano esperando.

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
§4.1.4 a §4.1.9 (`tasks/infra.yml`, `tasks/ecr.yml`, `tasks/batch.yml`,
`tasks/cluster.yml`, `tasks/mlflow_registry.yml`, `tasks/aws.yml`).
Esta seccion es el **catalogo de uso** de esas tasks ya creadas.

### 4.7.1 Re-entrenamiento (`task batch:retrain`)

Wrapper sobre el dispatcher Lambda + Batch. Submit + polling automatico
hasta `SUCCEEDED`/`FAILED`. Si pasas `WAIT=false`, dispara y vuelve.

```bash
# Re-entrenar POP en prod (espera hasta terminar)
task batch:retrain VARIETIES=POP

# Multi variedad (lanza N jobs en serie con espera entre cada uno)
task batch:retrain VARIETIES=POP,JUPITER

# Fire-and-forget (no esperes — el notifier ya manda mail)
task batch:retrain VARIETIES=POP WAIT=false

# prod_xl -> queue On-Demand (~5-6h, evita kills Spot)
task batch:retrain VARIETIES=POP TUNING=prod_xl
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
| `smoke.yml` | `task batch:smoke` | POP + tuning=smoke |
| `retrain.yml` | `task batch:retrain VARIETIES=...` | Multi-variedad serial |
| `scale_down.yml` | `task cluster:scale-down` o `task aws:sleep` | |
| `scale_up.yml` | `task cluster:scale-up` o `task aws:wake` | aws:wake incluye wait-healthy |
| `teardown.yml` | `task cluster:teardown` o `task aws:teardown` | |
| `rebuild.yml` | `task cluster:rebuild` o `task aws:rebuild` | |
| `promote.yml` | `task mlflow-aws:promote VARIETY=X VERSION=N` | Gate MAPE built-in |
| `bootstrap_cicd.yml` | (no necesario en V2) | El modulo `cicd` ya esta en `envs/prod/main.tf` |

Ver `task --list` para el catalogo completo incluyendo helpers
(`infra:output`, `infra:validate`, `batch:status`, `ecr:list`, ...).

---

> **Fin de la oleada 3 (Parte 4 — apply incremental + smoke + tasks operativas).**
>
> Estado actual:
> - Infra desplegada en AWS, ALB respondiendo 200.
> - Smoke test OK (1 job de Batch entreno POP, modelos en MLflow Registry y S3).
> - 6 archivos `tasks/*.yml` con ~30 tasks AWS expuestas en `task --list`:
>   `infra:*` (apply/destroy/plan/bootstrap), `ecr:*` (build-all/login),
>   `batch:*` (submit/retrain/smoke/wait/status), `cluster:*` (scale-up/down/teardown/rebuild),
>   `mlflow-aws:*` (promote/list-versions), `aws:*` (deploy/wake/sleep/destroy).
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
```

## 6.2 `.github/workflows/ci.yml` — lint + tests + build + push

Trigger: push a `main` o PR. Hace: lint (ruff + terraform fmt + task
syntax check), docker build, push a ECR como `latest` + `sha-<git-sha>`.

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch: {}

permissions:
  id-token: write       # requerido para OIDC
  contents: read

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
          cache: 'pip'

      - name: Install deps
        run: |
          pip install --upgrade pip
          pip install -r requirements.txt
          pip install ruff pytest pytest-cov

      - name: ruff (lint)
        run: ruff check src/ main.py scripts/

      - name: pytest (si existen tests)
        run: |
          if [ -d tests ]; then
            pytest tests/ --cov=src --cov-report=term-missing
          else
            echo "No tests/ dir, skip"
          fi

      - name: task validate (syntax check de todos los Taskfiles)
        uses: arduino/setup-task@v2
        with:
          version: 3.x
      - run: task --list-all > /dev/null

      - name: terraform fmt
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: 1.6.6
      - run: terraform fmt -check -recursive infra/

  build-and-push:
    needs: lint-and-test
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Assume gha-deploy role via OIDC
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_GHA_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}

      - name: ECR login
        uses: aws-actions/amazon-ecr-login@v2

      - name: Set tag
        id: tag
        run: |
          echo "sha=sha-$(git rev-parse --short=12 HEAD)" >> $GITHUB_OUTPUT
          echo "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> $GITHUB_OUTPUT

      - name: Build trainer image
        run: |
          docker build \
            --build-arg GIT_SHA=$(git rev-parse --short=12 HEAD) \
            --build-arg BUILD_DATE=${{ steps.tag.outputs.date }} \
            --build-arg VERSION=${{ steps.tag.outputs.sha }} \
            -t ${{ vars.ECR_TRAINER }}:latest \
            -t ${{ vars.ECR_TRAINER }}:${{ steps.tag.outputs.sha }} \
            -f Dockerfile .

      - name: Push trainer
        run: |
          docker push ${{ vars.ECR_TRAINER }}:latest
          docker push ${{ vars.ECR_TRAINER }}:${{ steps.tag.outputs.sha }}

      - name: Output image tag (for downstream workflows)
        run: |
          echo "::notice title=Pushed::${{ vars.ECR_TRAINER }}:${{ steps.tag.outputs.sha }}"
```

> **Checkpoint despues de pegar 6.2**: hace un push trivial (e.g.,
> editar README) y validar que el workflow corre verde:
>
> ```bash
> git commit --allow-empty -m "test: trigger ci.yml"
> git push origin main
> gh run list --workflow=ci.yml --limit 1
> # Esperado: status=completed, conclusion=success
> ```
>
> Si el workflow falla en "Assume gha-deploy role", el `gh variable
> set AWS_GHA_DEPLOY_ROLE_ARN` de §6.1 no se ejecuto o el ARN es
> incorrecto. Corregir antes de pasar a §6.3.

## 6.3 `.github/workflows/terraform-plan.yml` — validar PRs de infra

Cuando un PR toca `infra/**`, corre `terraform plan` y lo postea como
comment al PR.

```yaml
name: Terraform plan

on:
  pull_request:
    paths:
      - 'infra/**'
      - '.github/workflows/terraform-plan.yml'

permissions:
  id-token: write
  contents: read
  pull-requests: write

jobs:
  plan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Assume gha-deploy role
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_GHA_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}

      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: 1.6.6

      - name: terraform init
        working-directory: infra/envs/prod
        env:
          # Exportamos como env vars para que bash haga el slice
          # `${VAR: -6}` sobre un nombre de variable real (no se puede
          # hacer slice sobre el literal expandido de ${{ vars.X }}).
          AWS_ACCOUNT_ID: ${{ vars.AWS_ACCOUNT_ID }}
          PROJECT: ${{ vars.PROJECT }}
          AWS_REGION: ${{ vars.AWS_REGION }}
        run: |
          ACCOUNT_SUFFIX="${AWS_ACCOUNT_ID: -6}"
          terraform init \
            -backend-config="bucket=${PROJECT}-tfstate-${ACCOUNT_SUFFIX}" \
            -backend-config="key=envs/prod/terraform.tfstate" \
            -backend-config="region=${AWS_REGION}" \
            -backend-config="dynamodb_table=${PROJECT}-tflock"

      - name: terraform validate
        working-directory: infra/envs/prod
        run: terraform validate

      - name: terraform plan
        id: plan
        working-directory: infra/envs/prod
        run: terraform plan -no-color -out=tfplan
        continue-on-error: true

      - name: Comment plan en el PR
        uses: actions/github-script@v7
        with:
          script: |
            const output = `### Terraform plan
            \`\`\`
            ${{ steps.plan.outputs.stdout }}
            \`\`\`
            *exit code: ${{ steps.plan.outcome }}*`;
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: output
            });

      - name: Fail si plan fallo
        if: steps.plan.outcome == 'failure'
        run: exit 1
```

> **Checkpoint despues de pegar 6.3**: abre un PR que toque
> `infra/envs/prod/variables.tf` (cambio trivial, e.g., agregar un
> comentario) y verifica que el workflow corre y postea el plan
> como comment:
>
> ```bash
> gh run list --workflow=terraform-plan.yml --limit 1
> # Esperado: status=completed, conclusion=success
> gh pr view <PR#> --comments
> # Esperado: comment con el plan terraform como hidden details
> ```

## 6.4 `.github/workflows/train.yml` — entrenar desde la UI de GitHub

Workflow_dispatch con inputs (variety + tuning). Wakea servicios si
estan apagados, submitea el job, espera completion, y apaga si los
wakeo el mismo workflow.

```yaml
name: Train

on:
  workflow_dispatch:
    inputs:
      varieties:
        description: 'Variedades CSV o "all"'
        required: true
        default: 'POP'
        type: string
      tuning:
        description: 'Profile'
        required: true
        default: 'prod'
        type: choice
        options: [smoke, dev, prod, prod_xl]
      wait:
        description: 'Esperar hasta SUCCEEDED/FAILED'
        required: false
        default: true
        type: boolean

permissions:
  id-token: write
  contents: read

concurrency:
  # `inputs.X` (estilo nuevo) y `github.event.inputs.X` (estilo legacy)
  # apuntan al mismo valor en workflow_dispatch. Unificado a inputs.X
  # en todo este workflow.
  group: train-${{ inputs.varieties }}
  cancel-in-progress: false

jobs:
  train:
    runs-on: ubuntu-latest
    timeout-minutes: 480     # 8h (matchea job_attempt_seconds)
    steps:
      - name: Assume gha-train role
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_GHA_TRAIN_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}

      - name: Check MLflow up
        id: check
        run: |
          if curl -fs -o /dev/null --max-time 5 http://${{ vars.MLFLOW_ALB_DNS }}/health; then
            echo "needs_wake=false" >> $GITHUB_OUTPUT
            echo "::notice::MLflow up, skip wake"
          else
            echo "needs_wake=true" >> $GITHUB_OUTPUT
            echo "::notice::MLflow down, vamos a wake"
          fi
        continue-on-error: true

      # NOTA: requeriria gha-deploy role para invocar scheduler. Como simplificacion,
      # asumimos que el cron L-V 08-12 esta encendido o el user wakeo a mano via
      # task aws:wake. Para wake automatico via workflow, ver oleada 5.

      - name: Submit job via dispatcher
        id: submit
        run: |
          PAYLOAD=$(jq -nc \
            --arg v "${{ inputs.varieties }}" \
            --arg t "${{ inputs.tuning }}" \
            '{varieties: $v, tuning: $t}')
          aws lambda invoke \
            --function-name ${{ vars.PROJECT }}-dispatcher \
            --cli-binary-format raw-in-base64-out \
            --payload "$PAYLOAD" \
            /tmp/out.json \
            --query 'StatusCode' --output text
          cat /tmp/out.json
          JOB_ID=$(jq -r '.body.jobId' /tmp/out.json)
          echo "job_id=$JOB_ID" >> $GITHUB_OUTPUT
          echo "::notice title=Submitted::jobId=$JOB_ID"

      - name: Wait for completion
        if: ${{ inputs.wait }}
        run: |
          JOB_ID=${{ steps.submit.outputs.job_id }}
          while true; do
            STATUS=$(aws batch describe-jobs --jobs $JOB_ID \
                     --query 'jobs[0].status' --output text)
            echo "$(date -u): $STATUS"
            if [[ "$STATUS" == "SUCCEEDED" ]]; then
              echo "::notice title=Done::SUCCEEDED"
              exit 0
            fi
            if [[ "$STATUS" == "FAILED" ]]; then
              echo "::error title=Failed::Batch job FAILED"
              exit 1
            fi
            sleep 30
          done
```

Uso desde GitHub UI:

1. Actions → Train → Run workflow
2. Variety: `POP`
3. Tuning: `prod`
4. Wait: true (default)
5. Run workflow

GitHub Actions te muestra el log en tiempo real y `::notice` con el
jobId. Si elegis `wait=false`, retorna inmediatamente con el jobId y
podes verlo en CloudWatch.

> **Checkpoint despues de 6.4**: invoca el workflow desde la UI con
> `varieties=POP, tuning=smoke, wait=false` y valida que el dispatcher
> recibe el invoke:
>
> ```bash
> gh workflow run train.yml -f varieties=POP -f tuning=smoke -f wait=false
> gh run list --workflow=train.yml --limit 1
> # Esperado: status=completed (wait=false termina en <30s)
> aws logs tail /aws/lambda/ml-training-dispatcher --since 5m
> # Esperado: log con "SubmitJob OK" y el jobId del Batch
> ```

## 6.5 `.github/workflows/promote.yml` — Production transition con gate

```yaml
name: Promote model

on:
  workflow_dispatch:
    inputs:
      model_name:
        description: 'Nombre del modelo en Registry (ej: ml-training-POP)'
        required: true
        type: string
      version:
        description: 'Version a promover'
        required: true
        type: string
      max_mape:
        description: 'Umbral MAPE maximo aceptable (%)'
        required: false
        default: '20'
        type: string

permissions:
  id-token: write
  contents: read

jobs:
  promote:
    runs-on: ubuntu-latest
    environment: production    # requiere approval manual (configurar en GitHub Settings)
    steps:
      - uses: actions/checkout@v4

      - name: Assume gha-train role (solo lectura MLflow)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_GHA_TRAIN_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}

      - uses: actions/setup-python@v5
        with: { python-version: '3.13' }

      - run: pip install mlflow==3.12.0 requests

      - name: Quality gate (MAPE check)
        env:
          MLFLOW_TRACKING_URI: http://${{ vars.MLFLOW_ALB_DNS }}
        run: |
          python <<'PY'
          import mlflow, os, sys
          from mlflow.tracking import MlflowClient

          c = MlflowClient()
          name    = "${{ inputs.model_name }}"
          version = "${{ inputs.version }}"
          max_mape = float("${{ inputs.max_mape }}")

          mv = c.get_model_version(name=name, version=version)
          run = c.get_run(mv.run_id)
          mape = run.data.metrics.get("mape_oof") or run.data.metrics.get("mape")
          if mape is None:
              sys.exit(f"No 'mape_oof' ni 'mape' en run {mv.run_id}")
          if mape > max_mape:
              sys.exit(f"GATE FAIL: MAPE={mape:.3f} > umbral={max_mape}")
          print(f"GATE OK: MAPE={mape:.3f} <= {max_mape}")
          PY

      - name: A/B comparison contra Production actual
        env:
          MLFLOW_TRACKING_URI: http://${{ vars.MLFLOW_ALB_DNS }}
        run: |
          python <<'PY'
          from mlflow.tracking import MlflowClient
          c = MlflowClient()
          name    = "${{ inputs.model_name }}"
          version = "${{ inputs.version }}"

          # Candidato
          mv_new = c.get_model_version(name=name, version=version)
          run_new = c.get_run(mv_new.run_id)
          mape_new = run_new.data.metrics.get("mape_oof") or run_new.data.metrics.get("mape")

          # Production actual (si existe)
          prod = c.get_latest_versions(name, stages=["Production"])
          if not prod:
              print(f"No hay Production previo. Promoviendo {version} sin comparar.")
              raise SystemExit(0)

          mv_prod = prod[0]
          run_prod = c.get_run(mv_prod.run_id)
          mape_prod = run_prod.data.metrics.get("mape_oof") or run_prod.data.metrics.get("mape")

          import sys
          if mape_new >= mape_prod:
              sys.exit(f"GATE FAIL: candidato MAPE={mape_new:.3f} no mejora vs Production v{mv_prod.version} MAPE={mape_prod:.3f}")
          print(f"GATE OK: candidato MAPE={mape_new:.3f} mejora vs Production={mape_prod:.3f}")
          PY

      - name: Transition a Production
        env:
          MLFLOW_TRACKING_URI: http://${{ vars.MLFLOW_ALB_DNS }}
        run: |
          python <<'PY'
          from mlflow.tracking import MlflowClient
          c = MlflowClient()
          c.transition_model_version_stage(
              name="${{ inputs.model_name }}",
              version="${{ inputs.version }}",
              stage="Production",
              archive_existing_versions=True,
          )
          print("Transition OK")
          PY
```

> **Importante**: el job `promote` corre en `environment: production`.
> En GitHub Settings → Environments → production, configura "Required
> reviewers" para que requiera approval manual antes de correr. Asi
> el ultimo paso (transition) NO se ejecuta sin que vos clickees
> "Approve and deploy".

> **Checkpoint despues de 6.5**: ejecuta el workflow con
> `model=ml-training-POP, version=1` (necesitas al menos 1 version
> en Staging, generada por algun smoke previo). Confirma que pide
> approval antes del transition:
>
> ```bash
> gh workflow run promote.yml -f model=ml-training-POP -f version=1
> gh run watch  # te muestra el "Waiting for review" en vivo
> ```

## 6.6 Branch protection

```bash
# Required status checks: el job lint-and-test del ci.yml
gh api "repos/${GITHUB_OWNER}/ml_training/branches/main/protection" -X PUT --input - <<EOF
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["lint-and-test"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1
  },
  "restrictions": null
}
EOF
```

(O configurar via GitHub UI: Settings → Branches → Branch protection rules → Add rule.)

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

> **Fin de la oleada 4 (Partes 5-7 — patch trainer + CI/CD + promotion).**
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
> - **Parte 10**: hardening 🔮 FUTURO (TLS, WAF, Multi-AZ, KMS-CMK, VPC endpoints, DR).
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
task batch:retrain VARIETIES=POP TUNING=prod

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
    task batch:retrain VARIETIES=$v TUNING=prod || {
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
    task batch:retrain VARIETIES=$v TUNING=prod WAIT=false
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
- Si quota llena: pedir aumento (Parte 0.3.2).
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
task batch:retrain VARIETIES=POP
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
Si tenias bookmark, actualizalo. (Si en oleada 5 - Parte 10.1 -
agregaste un dominio custom via Route53, el dominio sigue igual; solo
el record A apunta al nuevo ALB.)

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

## 9.4 Optimizaciones adicionales (🔮 FUTURO)

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

# Parte 10 — Hardening production-grade (🔮 FUTURO)

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
optimizacion es "🔮 FUTURO" — aplicar cuando el trafico crezca o si
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
§0.3.3 — para mantener un unico entorno bash a lo largo de toda la
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
├── tasks/                              # NUEVO (orquestacion AWS)
│   ├── infra.yml                       # Parte 4.1.4   terraform + bootstrap
│   ├── ecr.yml                         # Parte 4.1.5   build + push 3 imagenes
│   ├── batch.yml                       # Parte 4.1.6   submit jobs + polling
│   ├── cluster.yml                     # Parte 4.1.7   lifecycle scale up/down
│   ├── mlflow_registry.yml             # Parte 4.1.8   promote con gate MAPE
│   └── aws.yml                         # Parte 4.1.9   orquestadores high-level
├── Taskfile.yml                        # MODIFICADO (Parte 4.1.3 anade includes:)
├── docker/                             # SOLO NUEVO el subdir reports/
│   └── reports/                        # Parte 3.6 (Dockerfile + nginx.conf + entrypoint.sh)
├── .github/workflows/                  # NUEVO
│   ├── ci.yml                          # Parte 6.2
│   ├── terraform-plan.yml              # Parte 6.3
│   ├── train.yml                       # Parte 6.4
│   ├── promote.yml                     # Parte 6.5
│   └── auto-train-on-push.yml          # Parte 13.2 (solo si aplicas Parte 13)
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

> **Fin de la oleada 5 y de la guia V2 completa.**
>
> Para usar esta guia desde el dia 1:
> 1. Leer la **Filosofia / por que cada oleada existe** (arriba).
> 2. Si nunca aplicaste nada: empezar en **Parte 0.3** (verificar prereqs).
> 3. Seguir lineal hasta **Parte 4.5** (smoke test). Eso te deja con
>    infra operativa y 1 job de Batch que entrena POP end-to-end.
> 4. Despues **Partes 5-7** para CI/CD + promotion (no son
>    indispensables el dia 1 pero conviene activarlos en semana 2).
> 5. **Parte 8** vivela como manual cuando algo falla; **Parte 11** es
>    la primera consulta cuando ves un error.
>
> Mantenimiento de esta guia: cada vez que cambies un modulo
> Terraform o un playbook, actualizar la seccion correspondiente +
> registrar el cambio en Apendice C (changelog V2.x).

---

# Parte 13 — Customizaciones puntuales (patches sobre Partes 1-12)

> **Por que esta parte existe**: las Partes 1-12 son la guia generica
> que sirve para cualquier deployment. Esta Parte 13 son los **5
> patches especificos** que pediste para TU caso de uso:
>
> 1. Scheduler L/Mi/V (no L-V) — Sec 13.1
> 2. Auto-train on push con wake/sleep — Sec 13.2
> 3. Orden serializado de wake (RDS → MLflow → Reports) — Sec 13.3
> 4. URLs locales documentadas (para que el dev sepa donde mirar local vs prod) — Sec 13.4
> 5. Como otro proyecto (FastAPI + Streamlit) consume este MLflow — Sec 13.5
>
> Aplicarlos DESPUES de que Oleadas 1-5 esten funcionando. Si los
> aplicas en mitad, podes dejar el state Terraform en estado
> inconsistente.

## 13.1 Scheduler L/Mi/V (en vez de L-V)

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

> **Fin de la Parte 13 — customizaciones aplicadas.**
>
> Estado final con los 5 patches:
> - Scheduler L/Mi/V 08-12 PET.
> - Push a main que toque `src/`, `main.py`, `Dockerfile`, `requirements.txt`
>   o `scripts/` -> auto-train con wake + train + cool-down 10 min + stop.
> - Wake serializado: RDS -> MLflow -> Reports.
> - Otro proyecto FastAPI/Streamlit con role IAM dedicado, snippet de
>   `load_production_model` y configuracion de env vars.
> - Costo: ~$63/mes (5 menos que el default L-V).

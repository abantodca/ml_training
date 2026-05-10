# Guia MLOps AWS — Infra modular en Terraform

> Repositorio destino sugerido: `ml-training-infra/` (separado del repo del trainer).
> Referencia rapida: `terraform init && terraform plan && terraform apply`.

Esta guia describe la infra de produccion como **modulos Terraform**: cada
modulo encapsula una capa (network, mlflow, batch, ...) con interface clara
(variables / outputs). El compose se hace en `envs/prod/main.tf`.

Diseño detras de la modularizacion:

1. **Aislar blast-radius**: tocar un modulo (p.ej. `batch`) no obliga a re-aplicar otro (p.ej. `mlflow`).
2. **Reutilizable**: el dia que necesites un `envs/dev/` o `envs/staging/`, copias el `prod/` y cambias `tfvars`.
3. **Idempotente**: `terraform apply` se puede correr N veces; el estado vive en S3 + lock en DynamoDB.
4. **Operacion declarativa**: la infra es codigo. PR = cambio de infra revisable.

---

## 0. Arquitectura objetivo

```
                                    ┌──────────────────────────────┐
                                    │  EventBridge                 │
                                    │  - cron 1 variedad/dia       │
                                    │  - S3 PutObject (data-raw)   │
                                    └────────────┬─────────────────┘
                                                 │
                                                 ▼
                                          ┌──────────────┐
                                          │  Lambda      │
                                          │  dispatcher  │
                                          └──────┬───────┘
                                                 │ batch:SubmitJob
                                                 ▼
                              ┌─────────────────────────────────────┐
                              │  AWS Batch                          │
                              │  ┌──────────────┐  ┌──────────────┐ │
                              │  │ queue-spot   │  │ queue-od     │ │
                              │  └──────┬───────┘  └──────┬───────┘ │
                              │  ┌──────▼───────┐  ┌──────▼───────┐ │
                              │  │ ce-spot      │  │ ce-ondemand  │ │
                              │  │ c6i.2xlarge  │  │ c6i.2xlarge  │ │
                              │  └──────────────┘  └──────────────┘ │
                              │  job-def: ml-training (8h, retry 2) │
                              └────────────────────┬────────────────┘
                                                   │
                  ┌────────────────────────────────┼────────────────────────────┐
                  ▼                                ▼                            ▼
       ┌──────────────────┐              ┌──────────────────┐         ┌─────────────────┐
       │ S3 data-raw      │              │ MLflow (Fargate) │         │ S3 mlflow-art   │
       │  +EventBridge    │              │  ALB :80 -> :5000│         │  artifacts/     │
       │  notifications   │              │  ↳ RDS Postgres  │         │  reports/       │
       └──────────────────┘              └──────────────────┘         └─────────────────┘

                              ┌──────────────────────────────────┐
                              │  CloudWatch Logs (retention 30d) │
                              │  + Alarms -> SNS -> Email/Slack  │
                              └──────────────────────────────────┘

                              ┌──────────────────────────────────┐
                              │  Lambda notifier (SUCCEEDED/FAIL)│
                              │  triggered by Batch state-change │
                              └──────────────────────────────────┘
```

**Operacion**: 1 variedad/dia, c6i.2xlarge satura sus 4 cores fisicos por
job. La queue Spot cubre `smoke|dev|prod`. La queue on-demand se usa
ad-hoc para `prod_xl` (5-6h) donde una interrupcion de Spot duele.

---

## 1. Estructura del repo de infra

```
ml-training-infra/
├── envs/
│   └── prod/
│       ├── main.tf              # composicion: llama a los 6 modulos
│       ├── variables.tf         # variables del entorno
│       ├── outputs.tf           # urls/arns para humanos
│       ├── versions.tf          # required_providers + aws default_tags
│       ├── backend.tf           # state remoto (S3 + DynamoDB lock)
│       └── terraform.tfvars     # valores reales (gitignored)
├── modules/
│   ├── network/      # VPC, 2x public/private subnets, NAT, IGW, SGs
│   ├── storage/      # S3 (data + artifacts) + ECR
│   ├── mlflow/       # RDS Postgres + ECS Fargate + ALB
│   ├── batch/        # 2 compute envs (spot + on-demand) + queues + job-def + IAM
│   ├── lambdas/      # dispatcher + notifier + EventBridge rules
│   └── monitoring/   # SNS topic + CW alarms
└── lambdas/
    ├── dispatcher/dispatcher.py
    └── notifier/notifier.py
```

Cada modulo tiene 3 archivos: `main.tf`, `variables.tf`, `outputs.tf`. Los
modulos `network` y `batch` ademas separan IAM en `iam.tf` para legibilidad.

**Convencion**: ningun modulo crea recursos por fuera de su capa. Si
`mlflow` necesita una subnet, la recibe como variable; no la crea.

---

## 2. Bootstrap del backend (UNA vez, antes de `terraform init`)

Terraform necesita un lugar donde guardar el state. Lo creamos a mano una
sola vez (no podemos terraform-izar el bucket que guarda nuestro propio
state — gallina y huevo). Despues, todo lo demas es Terraform puro.

```bash
PROJECT=ml-training
AWS_REGION=us-east-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
TF_BUCKET="${PROJECT}-tfstate-${ACCOUNT}"
TF_LOCK_TABLE="${PROJECT}-tflock"

aws s3api create-bucket --bucket ${TF_BUCKET} --region ${AWS_REGION}
aws s3api put-bucket-versioning --bucket ${TF_BUCKET} \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket ${TF_BUCKET} \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws dynamodb create-table \
  --table-name ${TF_LOCK_TABLE} \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region ${AWS_REGION}
```

Tambien creamos el SLR de Spot (necesario para que Batch use Spot Fleet):

```bash
aws iam create-service-linked-role --aws-service-name spotfleet.amazonaws.com 2>/dev/null || true
aws iam create-service-linked-role --aws-service-name batch.amazonaws.com 2>/dev/null || true
```

---

## 3. `envs/prod/` — la composicion

### 3.1 `versions.tf`

```hcl
terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws     = { source = "hashicorp/aws",     version = "~> 5.60" }
    archive = { source = "hashicorp/archive", version = "~> 2.4"  }
    random  = { source = "hashicorp/random",  version = "~> 3.6"  }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project
      ManagedBy   = "terraform"
      Environment = "prod"
    }
  }
}
```

### 3.2 `backend.tf`

```hcl
terraform {
  backend "s3" {
    bucket         = "ml-training-tfstate-123456789012"   # del bootstrap
    key            = "ml-training/prod/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "ml-training-tflock"
  }
}
```

### 3.3 `variables.tf`

```hcl
variable "project"            { type = string  default = "ml-training" }
variable "aws_region"         { type = string  default = "us-east-1" }
variable "vpc_cidr"           { type = string  default = "10.20.0.0/16" }
variable "alert_email"        { type = string }
variable "varieties_schedule" { type = list(string)  default = ["POP","JUPITER","VENTURA","SEKOYA","ALLISON","STELLA"] }
variable "default_tuning"     { type = string  default = "prod" }
variable "trainer_image_tag"  { type = string  default = "latest" }
variable "mlflow_image"       { type = string }                          # build via GH Actions
variable "rds_instance_class" { type = string  default = "db.t4g.micro" }
variable "spot_bid_percentage"{ type = number  default = 70 }
variable "job_attempt_seconds"{ type = number  default = 28800 }         # 8h
variable "log_retention_days" { type = number  default = 30 }
variable "mape_alarm_threshold" { type = number  default = 25 }
```

### 3.4 `terraform.tfvars` (gitignored)

```hcl
alert_email  = "tu-email@ejemplo.com"
mlflow_image = "123456789012.dkr.ecr.us-east-1.amazonaws.com/ml-training-mlflow:v3.12.0"
```

### 3.5 `main.tf` — la composicion

```hcl
module "network" {
  source   = "../../modules/network"
  project  = var.project
  vpc_cidr = var.vpc_cidr
}

module "storage" {
  source  = "../../modules/storage"
  project = var.project
}

module "mlflow" {
  source                   = "../../modules/mlflow"
  project                  = var.project
  vpc_id                   = module.network.vpc_id
  public_subnet_ids        = module.network.public_subnet_ids
  private_subnet_ids       = module.network.private_subnet_ids
  sg_alb_id                = module.network.sg_alb_id
  sg_mlflow_id             = module.network.sg_mlflow_id
  sg_rds_id                = module.network.sg_rds_id
  rds_instance_class       = var.rds_instance_class
  rds_allocated_storage_gb = 20
  mlflow_image             = var.mlflow_image
  artifacts_bucket         = module.storage.artifacts_bucket
  log_retention_days       = var.log_retention_days
}

module "batch" {
  source                = "../../modules/batch"
  project               = var.project
  private_subnet_ids    = module.network.private_subnet_ids
  sg_batch_id           = module.network.sg_batch_id
  ecr_trainer_url       = module.storage.ecr_trainer_url
  trainer_image_tag     = var.trainer_image_tag
  spot_bid_percentage   = var.spot_bid_percentage
  tracking_uri          = module.mlflow.tracking_uri
  artifacts_bucket      = module.storage.artifacts_bucket
  artifacts_bucket_arn  = module.storage.artifacts_bucket_arn
  data_bucket           = module.storage.data_bucket
  data_bucket_arn       = module.storage.data_bucket_arn
  job_attempt_seconds   = var.job_attempt_seconds
  log_retention_days    = var.log_retention_days
}

module "monitoring" {
  source               = "../../modules/monitoring"
  project              = var.project
  alert_email          = var.alert_email
  batch_job_queue_name = module.batch.job_queue_spot
  alb_arn              = module.mlflow.alb_arn
  tg_arn               = module.mlflow.tg_arn
  mape_alarm_threshold = var.mape_alarm_threshold
}

module "lambdas" {
  source                 = "../../modules/lambdas"
  project                = var.project
  job_queue_spot_arn     = module.batch.job_queue_spot_arn
  job_queue_ondemand_arn = module.batch.job_queue_ondemand_arn
  job_definition_arn     = module.batch.job_definition_arn
  job_definition_name    = module.batch.job_definition_name
  default_tuning         = var.default_tuning
  data_bucket            = module.storage.data_bucket
  data_bucket_arn        = module.storage.data_bucket_arn
  varieties_schedule     = var.varieties_schedule
  sns_topic_arn          = module.monitoring.sns_topic_arn
  log_retention_days     = var.log_retention_days
  lambdas_src_dir        = "${path.module}/../../lambdas"
}
```

### 3.6 `outputs.tf`

```hcl
output "mlflow_url"            { value = "http://${module.mlflow.alb_dns_name}" }
output "data_bucket"           { value = module.storage.data_bucket }
output "artifacts_bucket"      { value = module.storage.artifacts_bucket }
output "ecr_trainer_repo_url"  { value = module.storage.ecr_trainer_url }
output "batch_job_queue_spot"  { value = module.batch.job_queue_spot }
output "batch_job_queue_od"    { value = module.batch.job_queue_ondemand }
output "sns_alerts_topic"      { value = module.monitoring.sns_topic_arn }
```

---

## 4. Modulos

### 4.1 `modules/network/` — VPC + subnets + NAT + SGs

**Interface (variables)**: `project`, `vpc_cidr`, `azs` (opcional).
**Interface (outputs)**: `vpc_id`, `public_subnet_ids`, `private_subnet_ids`, `sg_{alb,mlflow,batch,rds}_id`.

`modules/network/main.tf`:

```hcl
data "aws_availability_zones" "available" { state = "available" }

locals {
  azs           = length(var.azs) > 0 ? var.azs : slice(data.aws_availability_zones.available.names, 0, 2)
  public_cidrs  = [cidrsubnet(var.vpc_cidr, 8, 0),  cidrsubnet(var.vpc_cidr, 8, 1)]
  private_cidrs = [cidrsubnet(var.vpc_cidr, 8, 10), cidrsubnet(var.vpc_cidr, 8, 11)]
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.project}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.project}-igw" }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.public_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
  tags = { Name = "${var.project}-public-${count.index}", Tier = "public" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags              = { Name = "${var.project}-private-${count.index}", Tier = "private" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${var.project}-nat-eip" }
}

# Single-AZ NAT GW para abaratar. Si hace falta HA, duplicar NAT GW + RT.
resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = "${var.project}-nat" }
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route { cidr_block = "0.0.0.0/0"  gateway_id = aws_internet_gateway.this.id }
  tags  = { Name = "${var.project}-rt-public" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  route { cidr_block = "0.0.0.0/0"  nat_gateway_id = aws_nat_gateway.this.id }
  tags  = { Name = "${var.project}-rt-private" }
}

resource "aws_route_table_association" "public"  { count = 2  subnet_id = aws_subnet.public[count.index].id   route_table_id = aws_route_table.public.id }
resource "aws_route_table_association" "private" { count = 2  subnet_id = aws_subnet.private[count.index].id  route_table_id = aws_route_table.private.id }

# ─── Security groups (egress libre; ingress por SG-rules separadas) ──────

resource "aws_security_group" "alb"    { name = "${var.project}-alb"    vpc_id = aws_vpc.this.id  egress { from_port=0  to_port=0  protocol="-1"  cidr_blocks=["0.0.0.0/0"] }  ingress { from_port=80  to_port=80  protocol="tcp"  cidr_blocks=["0.0.0.0/0"] } }
resource "aws_security_group" "mlflow" { name = "${var.project}-mlflow" vpc_id = aws_vpc.this.id  egress { from_port=0  to_port=0  protocol="-1"  cidr_blocks=["0.0.0.0/0"] } }
resource "aws_security_group" "batch"  { name = "${var.project}-batch"  vpc_id = aws_vpc.this.id  egress { from_port=0  to_port=0  protocol="-1"  cidr_blocks=["0.0.0.0/0"] } }
resource "aws_security_group" "rds"    { name = "${var.project}-rds"    vpc_id = aws_vpc.this.id  egress { from_port=0  to_port=0  protocol="-1"  cidr_blocks=["0.0.0.0/0"] } }

resource "aws_security_group_rule" "mlflow_from_alb"   { type="ingress"  from_port=5000 to_port=5000 protocol="tcp" source_security_group_id=aws_security_group.alb.id    security_group_id=aws_security_group.mlflow.id }
resource "aws_security_group_rule" "mlflow_from_batch" { type="ingress"  from_port=5000 to_port=5000 protocol="tcp" source_security_group_id=aws_security_group.batch.id  security_group_id=aws_security_group.mlflow.id }
resource "aws_security_group_rule" "rds_from_mlflow"   { type="ingress"  from_port=5432 to_port=5432 protocol="tcp" source_security_group_id=aws_security_group.mlflow.id security_group_id=aws_security_group.rds.id }
```

### 4.2 `modules/storage/` — S3 + ECR

**Interface**: solo `project`. Outputs: `data_bucket`, `artifacts_bucket`, `ecr_trainer_url`, `ecr_mlflow_url` (+ ARNs).

```hcl
data "aws_caller_identity" "current" {}

locals {
  account_short = substr(data.aws_caller_identity.current.account_id, -6, 6)
  data_bucket   = "${var.project}-data-${local.account_short}"
  mlflow_bucket = "${var.project}-mlflow-${local.account_short}"
}

resource "aws_s3_bucket"          "data"   { bucket = local.data_bucket }
resource "aws_s3_bucket"          "mlflow" { bucket = local.mlflow_bucket }

# Versioning + SSE + block-public en los DOS buckets
resource "aws_s3_bucket_versioning"                       "data"   { bucket = aws_s3_bucket.data.id    versioning_configuration { status = "Enabled" } }
resource "aws_s3_bucket_versioning"                       "mlflow" { bucket = aws_s3_bucket.mlflow.id  versioning_configuration { status = "Enabled" } }
resource "aws_s3_bucket_server_side_encryption_configuration" "data"   { bucket = aws_s3_bucket.data.id    rule { apply_server_side_encryption_by_default { sse_algorithm = "AES256" } } }
resource "aws_s3_bucket_server_side_encryption_configuration" "mlflow" { bucket = aws_s3_bucket.mlflow.id  rule { apply_server_side_encryption_by_default { sse_algorithm = "AES256" } } }
resource "aws_s3_bucket_public_access_block" "data"   { bucket = aws_s3_bucket.data.id    block_public_acls=true block_public_policy=true ignore_public_acls=true restrict_public_buckets=true }
resource "aws_s3_bucket_public_access_block" "mlflow" { bucket = aws_s3_bucket.mlflow.id  block_public_acls=true block_public_policy=true ignore_public_acls=true restrict_public_buckets=true }

# data-raw emite eventos a EventBridge (consume el dispatcher Lambda)
resource "aws_s3_bucket_notification" "data_eventbridge" {
  bucket      = aws_s3_bucket.data.id
  eventbridge = true
}

# Lifecycle: borrar versiones antiguas (90d data, 180d mlflow)
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule { id="expire-old"  status="Enabled"  filter {}  noncurrent_version_expiration { noncurrent_days = 90 } }
}
resource "aws_s3_bucket_lifecycle_configuration" "mlflow" {
  bucket = aws_s3_bucket.mlflow.id
  rule { id="expire-old"  status="Enabled"  filter {}  noncurrent_version_expiration { noncurrent_days = 180 } }
}

# ECR repos: trainer + mlflow custom (con scan-on-push)
resource "aws_ecr_repository" "trainer" { name = var.project              image_scanning_configuration { scan_on_push = true } }
resource "aws_ecr_repository" "mlflow"  { name = "${var.project}-mlflow"  image_scanning_configuration { scan_on_push = true } }

resource "aws_ecr_lifecycle_policy" "trainer" {
  repository = aws_ecr_repository.trainer.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged after 7 days"
      selection    = { tagStatus = "untagged"  countType = "sinceImagePushed"  countUnit = "days"  countNumber = 7 }
      action       = { type = "expire" }
    }]
  })
}
```

### 4.3 `modules/mlflow/` — RDS + ECS Fargate + ALB

**Interface**: subnets + SGs (de network), `mlflow_image`, `artifacts_bucket`. Outputs: `alb_dns_name`, `tracking_uri`, `cluster_name`, `service_name`.

```hcl
# RDS Postgres + Secret
resource "random_password" "db" {
  length           = 24
  special          = true
  override_special = "!#$%&*-_=+"      # evitar @ y espacios para la URI de MLflow
}

resource "aws_secretsmanager_secret"        "db" { name = "${var.project}/mlflow/db"  recovery_window_in_days = 0 }
resource "aws_secretsmanager_secret_version" "db" { secret_id = aws_secretsmanager_secret.db.id  secret_string = jsonencode({ username = "mlflow", password = random_password.db.result }) }

resource "aws_db_subnet_group" "mlflow" { name = "${var.project}-mlflow"  subnet_ids = var.private_subnet_ids }

resource "aws_db_instance" "mlflow" {
  identifier             = "${var.project}-mlflow"
  engine                 = "postgres"
  engine_version         = "15.7"
  instance_class         = var.rds_instance_class
  allocated_storage      = var.rds_allocated_storage_gb
  storage_type           = "gp3"
  storage_encrypted      = true
  db_name                = "mlflow"
  username               = "mlflow"
  password               = random_password.db.result
  vpc_security_group_ids = [var.sg_rds_id]
  db_subnet_group_name   = aws_db_subnet_group.mlflow.name
  multi_az               = false
  publicly_accessible    = false
  backup_retention_period = 7
  skip_final_snapshot    = true
  apply_immediately      = true
}

# ALB (publico) + TG + listener
resource "aws_lb"               "mlflow" { name = "${var.project}-mlflow"  internal=false  load_balancer_type="application"  security_groups=[var.sg_alb_id]  subnets=var.public_subnet_ids }
resource "aws_lb_target_group"  "mlflow" { name = "${var.project}-mlflow"  port=5000  protocol="HTTP"  target_type="ip"  vpc_id=var.vpc_id  health_check { path="/health"  matcher="200"  interval=30 } }
resource "aws_lb_listener"      "mlflow" { load_balancer_arn = aws_lb.mlflow.arn  port=80  protocol="HTTP"  default_action { type="forward"  target_group_arn=aws_lb_target_group.mlflow.arn } }

# ECS cluster + task-def + service
resource "aws_ecs_cluster"            "this"   { name = "${var.project}-cluster" }
resource "aws_cloudwatch_log_group"   "mlflow" { name = "/aws/ecs/${var.project}-mlflow"  retention_in_days = var.log_retention_days }

resource "aws_iam_role" "ecs_exec" {
  name = "${var.project}-ecs-exec"
  assume_role_policy = jsonencode({ Version="2012-10-17" Statement=[{ Effect="Allow" Principal={ Service="ecs-tasks.amazonaws.com" } Action="sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "ecs_exec" { role = aws_iam_role.ecs_exec.name  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy" }

resource "aws_iam_role" "mlflow_task" {
  name = "${var.project}-mlflow-task"
  assume_role_policy = jsonencode({ Version="2012-10-17" Statement=[{ Effect="Allow" Principal={ Service="ecs-tasks.amazonaws.com" } Action="sts:AssumeRole" }] })
}
resource "aws_iam_role_policy" "mlflow_task_s3" {
  name = "s3-artifacts"  role = aws_iam_role.mlflow_task.id
  policy = jsonencode({ Version="2012-10-17" Statement=[{ Effect="Allow" Action=["s3:ListBucket","s3:GetObject","s3:PutObject","s3:DeleteObject"]
                        Resource = ["arn:aws:s3:::${var.artifacts_bucket}", "arn:aws:s3:::${var.artifacts_bucket}/*"] }] })
}

locals {
  alb_dns        = aws_lb.mlflow.dns_name
  allowed_hosts  = "${local.alb_dns},${local.alb_dns}:*,localhost,localhost:*,127.0.0.1,127.0.0.1:*"
  backend_db_uri = "postgresql://mlflow:${random_password.db.result}@${aws_db_instance.mlflow.endpoint}/mlflow"
  artifact_root  = "s3://${var.artifacts_bucket}/artifacts"
}

resource "aws_ecs_task_definition" "mlflow" {
  family                   = "${var.project}-mlflow"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu = "512"  memory = "1024"
  execution_role_arn = aws_iam_role.ecs_exec.arn
  task_role_arn      = aws_iam_role.mlflow_task.arn

  container_definitions = jsonencode([{
    name      = "mlflow"
    image     = var.mlflow_image
    essential = true
    portMappings = [{ containerPort = 5000, protocol = "tcp" }]
    command = [
      "mlflow", "server",
      "--host", "0.0.0.0", "--port", "5000",
      "--allowed-hosts", local.allowed_hosts,
      "--backend-store-uri", local.backend_db_uri,
      "--default-artifact-root", local.artifact_root,
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.mlflow.name
        awslogs-region        = data.aws_region.current.name
        awslogs-stream-prefix = "mlflow"
      }
    }
  }])
}

resource "aws_ecs_service" "mlflow" {
  name            = "${var.project}-mlflow"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.mlflow.arn
  desired_count   = 1
  launch_type     = "FARGATE"

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

  depends_on = [aws_lb_listener.mlflow]
}
```

### 4.4 `modules/batch/` — compute envs + queues + job-def + IAM

**Interface**: `private_subnet_ids`, `sg_batch_id`, `ecr_trainer_url`, `tracking_uri`, los buckets de S3, `job_attempt_seconds`. Outputs: `job_queue_spot`, `job_queue_ondemand`, `job_definition_name` (+ ARNs).

`modules/batch/iam.tf`:

```hcl
data "aws_region"          "current" {}
data "aws_caller_identity" "current" {}

# Instance profile (host EC2 de Batch)
resource "aws_iam_role" "instance" {
  name = "${var.project}-batch-instance"
  assume_role_policy = jsonencode({ Version="2012-10-17" Statement=[{ Effect="Allow" Principal={ Service="ec2.amazonaws.com" } Action="sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "instance" { role = aws_iam_role.instance.name  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role" }
resource "aws_iam_instance_profile"       "instance" { name = "${var.project}-batch-instance"  role = aws_iam_role.instance.name }

# Execution role (lo asume el container al arrancar)
resource "aws_iam_role" "exec" {
  name = "${var.project}-batch-exec"
  assume_role_policy = jsonencode({ Version="2012-10-17" Statement=[{ Effect="Allow" Principal={ Service="ecs-tasks.amazonaws.com" } Action="sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "exec" { role = aws_iam_role.exec.name  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy" }

# Job role (lo que hace el codigo del trainer)
resource "aws_iam_role" "job" {
  name = "${var.project}-batch-job"
  assume_role_policy = jsonencode({ Version="2012-10-17" Statement=[{ Effect="Allow" Principal={ Service="ecs-tasks.amazonaws.com" } Action="sts:AssumeRole" }] })
}
resource "aws_iam_role_policy" "job_s3" {
  name = "s3-data-and-artifacts"  role = aws_iam_role.job.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = [var.data_bucket_arn, var.artifacts_bucket_arn] },
      { Effect = "Allow", Action = ["s3:GetObject","s3:PutObject","s3:DeleteObject"],
        Resource = ["${var.data_bucket_arn}/*", "${var.artifacts_bucket_arn}/*"] }
    ]
  })
}
resource "aws_iam_role_policy" "job_cw_metrics" {
  name = "cw-metrics-emit"  role = aws_iam_role.job.id
  policy = jsonencode({ Version="2012-10-17" Statement=[{ Effect="Allow" Action=["cloudwatch:PutMetricData"] Resource="*" Condition={ StringEquals={ "cloudwatch:namespace" = "MLTraining" } } }] })
}
```

`modules/batch/main.tf`:

```hcl
resource "aws_cloudwatch_log_group" "batch" {
  name              = "/aws/batch/${var.project}"
  retention_in_days = var.log_retention_days
}

resource "aws_batch_compute_environment" "spot" {
  compute_environment_name = "${var.project}-ce-spot"
  type   = "MANAGED"  state = "ENABLED"
  compute_resources {
    type                = "EC2"
    allocation_strategy = "BEST_FIT_PROGRESSIVE"
    bid_percentage      = var.spot_bid_percentage
    min_vcpus           = 0  max_vcpus = var.spot_max_vcpus  desired_vcpus = 0
    instance_type       = [var.instance_type]
    subnets             = var.private_subnet_ids
    security_group_ids  = [var.sg_batch_id]
    instance_role       = aws_iam_instance_profile.instance.arn
    spot_iam_fleet_role = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-service-role/spotfleet.amazonaws.com/AWSServiceRoleForEC2SpotFleet"
  }
}

resource "aws_batch_compute_environment" "ondemand" {
  compute_environment_name = "${var.project}-ce-ondemand"
  type   = "MANAGED"  state = "ENABLED"
  compute_resources {
    type                = "EC2"
    allocation_strategy = "BEST_FIT_PROGRESSIVE"
    min_vcpus           = 0  max_vcpus = var.ondemand_max_vcpus  desired_vcpus = 0
    instance_type       = [var.instance_type]
    subnets             = var.private_subnet_ids
    security_group_ids  = [var.sg_batch_id]
    instance_role       = aws_iam_instance_profile.instance.arn
  }
}

resource "aws_batch_job_queue" "spot"     { name = "${var.project}-queue"           state = "ENABLED" priority = 100  compute_environment_order { order=1  compute_environment = aws_batch_compute_environment.spot.arn } }
resource "aws_batch_job_queue" "ondemand" { name = "${var.project}-queue-ondemand"  state = "ENABLED" priority = 100  compute_environment_order { order=1  compute_environment = aws_batch_compute_environment.ondemand.arn } }

resource "aws_batch_job_definition" "trainer" {
  name = var.project
  type = "container"
  platform_capabilities = ["EC2"]

  retry_strategy { attempts = 2 }
  timeout        { attempt_duration_seconds = var.job_attempt_seconds }   # 8h cubre prod_xl

  container_properties = jsonencode({
    image            = "${var.ecr_trainer_url}:${var.trainer_image_tag}"
    command          = ["--varieties", "POP", "--tuning", "prod"]         # default; el dispatcher lo override-a
    executionRoleArn = aws_iam_role.exec.arn
    jobRoleArn       = aws_iam_role.job.arn
    resourceRequirements = [
      { type = "VCPU",   value = "8" },
      { type = "MEMORY", value = "15000" },
    ]
    environment = [
      { name = "MLFLOW_TRACKING_URI", value = var.tracking_uri },
      { name = "S3_ARTIFACTS_BUCKET", value = var.artifacts_bucket },
      { name = "S3_ARTIFACTS_PREFIX", value = "artifacts" },
      { name = "S3_REPORTS_PREFIX",   value = "reports" },
      { name = "AWS_DEFAULT_REGION",  value = data.aws_region.current.name },
      { name = "EMIT_CW_METRICS",     value = "1" },                      # activa metric custom para alarma de MAPE
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.batch.name
        awslogs-region        = data.aws_region.current.name
        awslogs-stream-prefix = "job"
      }
    }
  })
}
```

### 4.5 `modules/lambdas/` — dispatcher + notifier + EventBridge

**Interface**: `job_queue_*_arn`, `job_definition_*`, `varieties_schedule`, `data_bucket`, `sns_topic_arn`, `lambdas_src_dir`. Sin outputs criticos para otros modulos.

`modules/lambdas/dispatcher.tf`:

```hcl
data "aws_region"          "current" {}
data "aws_caller_identity" "current" {}

data "archive_file" "dispatcher" {
  type        = "zip"
  source_dir  = "${var.lambdas_src_dir}/dispatcher"
  output_path = "${path.module}/.build/dispatcher.zip"
}

resource "aws_iam_role" "dispatcher" {
  name = "${var.project}-dispatcher"
  assume_role_policy = jsonencode({ Version="2012-10-17" Statement=[{ Effect="Allow" Principal={ Service="lambda.amazonaws.com" } Action="sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "dispatcher_basic" { role = aws_iam_role.dispatcher.name  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" }
resource "aws_iam_role_policy" "dispatcher_submit" {
  name = "batch-submit"  role = aws_iam_role.dispatcher.id
  policy = jsonencode({ Version="2012-10-17" Statement=[{
    Effect = "Allow"  Action = ["batch:SubmitJob"]
    Resource = [
      var.job_queue_spot_arn, var.job_queue_ondemand_arn,
      "arn:aws:batch:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:job-definition/${var.job_definition_name}*",
    ]
  }] })
}

resource "aws_cloudwatch_log_group" "dispatcher" { name = "/aws/lambda/${var.project}-dispatcher"  retention_in_days = var.log_retention_days }

resource "aws_lambda_function" "dispatcher" {
  function_name    = "${var.project}-dispatcher"
  role             = aws_iam_role.dispatcher.arn
  filename         = data.archive_file.dispatcher.output_path
  source_code_hash = data.archive_file.dispatcher.output_base64sha256
  runtime          = "python3.13"
  handler          = "dispatcher.lambda_handler"
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      JOB_QUEUE           = element(reverse(split("/", var.job_queue_spot_arn)), 0)
      JOB_DEFINITION      = var.job_definition_name
      DEFAULT_TUNING      = var.default_tuning
      DEFAULT_VARIETIES   = "POP"
      DEFAULT_DATA_BUCKET = var.data_bucket
      DEFAULT_DATA_KEY    = "latest/BD_HISTORICO_ACUMULADO.xlsx"
    }
  }
  depends_on = [aws_cloudwatch_log_group.dispatcher]
}

# 1 cron por variedad (1 variedad/dia, dias 5..N del mes a las 06:00 UTC)
resource "aws_cloudwatch_event_rule" "schedule" {
  for_each            = { for i, v in var.varieties_schedule : v => i }
  name                = "${var.project}-train-${lower(each.key)}"
  schedule_expression = "cron(0 6 ${5 + each.value} * ? *)"
  state               = "ENABLED"
}
resource "aws_cloudwatch_event_target" "schedule" {
  for_each = aws_cloudwatch_event_rule.schedule
  rule     = each.value.name
  arn      = aws_lambda_function.dispatcher.arn
  input    = jsonencode({ detail = { varieties = each.key, tuning = var.default_tuning } })
}
resource "aws_lambda_permission" "schedule" {
  for_each      = aws_cloudwatch_event_rule.schedule
  statement_id  = "eb-schedule-${lower(each.key)}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dispatcher.function_name
  principal     = "events.amazonaws.com"
  source_arn    = each.value.arn
}

# Trigger por upload a S3 data-raw
resource "aws_cloudwatch_event_rule" "s3_upload" {
  name        = "${var.project}-data-uploaded"
  state       = "ENABLED"
  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [var.data_bucket] }
      object = { key = [{ prefix = "incoming/", suffix = "BD_HISTORICO_ACUMULADO.xlsx" }] }
    }
  })
}
resource "aws_cloudwatch_event_target" "s3_upload" { rule = aws_cloudwatch_event_rule.s3_upload.name  arn = aws_lambda_function.dispatcher.arn }
resource "aws_lambda_permission"      "s3_upload" { statement_id="eb-s3-upload" action="lambda:InvokeFunction" function_name=aws_lambda_function.dispatcher.function_name principal="events.amazonaws.com" source_arn=aws_cloudwatch_event_rule.s3_upload.arn }
```

`modules/lambdas/notifier.tf` — analogo al dispatcher pero con event-pattern de Batch state-change (`SUCCEEDED`/`FAILED`).

### 4.6 Codigo Python de las Lambdas (`lambdas/`)

`lambdas/dispatcher/dispatcher.py`:

```python
"""Lambda dispatcher: EventBridge -> AWS Batch SubmitJob."""
import os, re
import boto3

batch = boto3.client("batch")
JOB_QUEUE         = os.environ["JOB_QUEUE"]
JOB_DEFINITION    = os.environ["JOB_DEFINITION"]
DEFAULT_TUNING    = os.environ.get("DEFAULT_TUNING", "prod")
DEFAULT_VARIETIES = os.environ.get("DEFAULT_VARIETIES", "POP")
DEFAULT_BUCKET    = os.environ.get("DEFAULT_DATA_BUCKET", "")
DEFAULT_KEY       = os.environ.get("DEFAULT_DATA_KEY", "")

def _from_schedule(detail):
    return (detail.get("varieties", DEFAULT_VARIETIES),
            detail.get("tuning", DEFAULT_TUNING),
            detail.get("s3_data_bucket", DEFAULT_BUCKET),
            detail.get("s3_data_key", DEFAULT_KEY))

def _from_s3(detail):
    # NO hardcodeamos "all": un upload de Excel dispara DEFAULT_VARIETIES.
    return DEFAULT_VARIETIES, DEFAULT_TUNING, detail["bucket"]["name"], detail["object"]["key"]

def lambda_handler(event, context):
    detail = event.get("detail", {})
    if event.get("source") == "aws.s3":
        varieties, tuning, s3_bucket, s3_key = _from_s3(detail)
    else:
        varieties, tuning, s3_bucket, s3_key = _from_schedule(detail)

    if not (s3_bucket and s3_key):
        raise ValueError("Falta S3_DATA_BUCKET/S3_DATA_KEY")

    job_name = re.sub(r"[^a-zA-Z0-9_-]", "-", f"train-{varieties}-{tuning}")[:128]
    resp = batch.submit_job(
        jobName=job_name, jobQueue=JOB_QUEUE, jobDefinition=JOB_DEFINITION,
        containerOverrides={
            "command": ["--varieties", varieties, "--tuning", tuning],
            "environment": [{"name":"S3_DATA_BUCKET","value":s3_bucket},
                            {"name":"S3_DATA_KEY","value":s3_key}],
        })
    return {"jobId": resp["jobId"], "varieties": varieties, "tuning": tuning,
            "s3_data": f"s3://{s3_bucket}/{s3_key}"}
```

`lambdas/notifier/notifier.py`:

```python
"""Lambda notifier: Batch state-change -> SNS."""
import os
import boto3

sns = boto3.client("sns")
TOPIC_ARN = os.environ["TOPIC_ARN"]

def lambda_handler(event, context):
    detail = event["detail"]
    status = detail["status"]
    if status not in ("SUCCEEDED", "FAILED"):
        return {"skipped": status}
    msg = (f"[ml-training] {status}\n"
           f"jobName={detail.get('jobName','?')}\n"
           f"jobId={detail.get('jobId','?')}\n"
           f"command={' '.join(detail.get('container',{}).get('command',[]))}\n")
    if status == "FAILED":
        msg += f"reason={detail.get('statusReason','(sin razon)')}\n"
    sns.publish(TopicArn=TOPIC_ARN, Subject=f"ml-training {status}", Message=msg)
    return {"published": True}
```

### 4.7 `modules/monitoring/` — SNS + alarmas

**Interface**: `alert_email`, `batch_job_queue_name`, `alb_arn`, `tg_arn`, `mape_alarm_threshold`. Output: `sns_topic_arn`.

```hcl
resource "aws_sns_topic"              "alerts" { name = "${var.project}-alerts" }
resource "aws_sns_topic_subscription" "email"  { topic_arn = aws_sns_topic.alerts.arn  protocol = "email"  endpoint = var.alert_email }
# (la suscripcion email requiere confirmacion manual desde el inbox)

# Alarma 1: cualquier job FAILED en la ultima hora
resource "aws_cloudwatch_metric_alarm" "job_failures" {
  alarm_name = "${var.project}-job-failures"
  namespace  = "AWS/Batch"  metric_name = "FailedJobCount"  statistic = "Sum"
  period     = 3600  evaluation_periods = 1
  threshold  = 0  comparison_operator = "GreaterThanThreshold"
  treat_missing_data = "notBreaching"
  dimensions = { JobQueue = var.batch_job_queue_name }
  alarm_actions = [aws_sns_topic.alerts.arn]
}

# Alarma 2: degradacion del modelo (MAPE > umbral) — se alimenta del custom metric
# que el trainer emite con EMIT_CW_METRICS=1 (ver §6).
resource "aws_cloudwatch_metric_alarm" "mape_pop" {
  alarm_name = "${var.project}-mape-pop-too-high"
  namespace  = "MLTraining"  metric_name = "BusinessMAPE"  statistic = "Average"
  period     = 86400  evaluation_periods = 1
  threshold  = var.mape_alarm_threshold  comparison_operator = "GreaterThanThreshold"
  treat_missing_data = "notBreaching"
  dimensions = { Variety = "POP" }
  alarm_actions = [aws_sns_topic.alerts.arn]
}

# Alarma 3: MLflow Fargate unhealthy 5+ minutos
locals {
  alb_name      = element(split("/", var.alb_arn), length(split("/", var.alb_arn)) - 2)
  alb_id        = element(split("/", var.alb_arn), length(split("/", var.alb_arn)) - 1)
  tg_name       = element(split("/", var.tg_arn),  length(split("/", var.tg_arn))  - 2)
  tg_id         = element(split("/", var.tg_arn),  length(split("/", var.tg_arn))  - 1)
}
resource "aws_cloudwatch_metric_alarm" "mlflow_unhealthy" {
  alarm_name = "${var.project}-mlflow-unhealthy"
  namespace  = "AWS/ApplicationELB"  metric_name = "UnHealthyHostCount"  statistic = "Maximum"
  period     = 60  evaluation_periods = 5
  threshold  = 0  comparison_operator = "GreaterThanThreshold"
  treat_missing_data = "notBreaching"
  dimensions = {
    TargetGroup  = "targetgroup/${local.tg_name}/${local.tg_id}"
    LoadBalancer = "app/${local.alb_name}/${local.alb_id}"
  }
  alarm_actions = [aws_sns_topic.alerts.arn]
}
```

---

## 5. Workflow

```bash
cd ml-training-infra/envs/prod

# Una sola vez (descarga providers + inicializa state remoto)
terraform init

# Preview de cambios
terraform plan -var-file=terraform.tfvars

# Apply
terraform apply -var-file=terraform.tfvars

# Outputs (URLs y nombres importantes)
terraform output
```

Despues de cada cambio en `*.tf` o `lambdas/*.py`:

```bash
terraform plan
terraform apply
```

`apply` es idempotente: re-correrlo sin cambios da "0 to change". Cambios
en lambdas (.py) se detectan por `source_code_hash` y disparan re-deploy
automatico.

---

## 6. Codigo del trainer — patches necesarios

El repo del trainer (`ml_training/`) ya respeta `MLFLOW_TRACKING_URI`,
`S3_ARTIFACTS_BUCKET`, etc. **Cambio unico necesario** para activar la
alarma de MAPE: emitir custom metric a CloudWatch al final del run.

Patch en `main.py` (despues de `_write_aggregate_summary`):

```python
def _emit_mape_metric(aggregate_path):
    """Emite business_oof_mape de cada variedad como custom metric en CW."""
    if not os.environ.get("EMIT_CW_METRICS"):
        return
    import json
    import boto3
    cw = boto3.client("cloudwatch")
    data = json.loads(open(aggregate_path).read())
    metric_data = []
    for variety, info in data.get("per_variety", {}).items():
        ch = info.get("champion") or {}
        mape = ch.get("champion_mape_oof_business")
        if mape is not None:
            metric_data.append({
                "MetricName": "BusinessMAPE",
                "Dimensions": [{"Name": "Variety", "Value": variety}],
                "Value": float(mape),
                "Unit": "Percent",
            })
    if metric_data:
        cw.put_metric_data(Namespace="MLTraining", MetricData=metric_data)
```

`EMIT_CW_METRICS=1` ya queda inyectado por la job-def (modulo `batch`).

---

## 7. CI/CD del trainer (GitHub Actions)

El IaC NO maneja la imagen del trainer — la construye GH Actions y la
pushea a ECR. Tag = `git sha` corto.

`.github/workflows/build.yml`:

```yaml
name: build-and-push
on:
  push:
    branches: [main]
permissions:
  id-token: write
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::ACCOUNT:role/gha-ecr-push
          aws-region: us-east-1
      - id: ecr
        uses: aws-actions/amazon-ecr-login@v2
      - name: Build & push
        run: |
          IMG=${{ steps.ecr.outputs.registry }}/ml-training:${GITHUB_SHA::7}
          docker buildx build --platform linux/amd64 -t $IMG --push .
      - name: Update job-def via terraform
        run: |
          cd infra/envs/prod
          terraform apply -auto-approve \
            -var="trainer_image_tag=${GITHUB_SHA::7}"
```

`gha-ecr-push` es un IAM role con OIDC trust de GitHub + permisos minimos
(ECR push + Terraform state RW). Lo crea **otro** modulo `cicd/` que
podes agregar mas adelante; no es bloqueante para operar.

---

## 8. Runbook

### 8.1 Re-entrenar manualmente una variedad

```bash
QUEUE=$(terraform -chdir=envs/prod output -raw batch_job_queue_spot)
DEF=$(terraform -chdir=envs/prod   output -raw batch_job_definition)

aws batch submit-job \
  --job-name "manual-$(date +%s)" \
  --job-queue ${QUEUE} \
  --job-definition ${DEF} \
  --container-overrides '{"command":["--varieties","JUPITER","--tuning","prod"]}'
```

### 8.2 Recovery: re-entrenar TODAS en un dia (loop manual)

```bash
QUEUE=$(terraform -chdir=envs/prod output -raw batch_job_queue_spot)
DEF=$(terraform -chdir=envs/prod   output -raw batch_job_definition)

for VARIETY in POP JUPITER VENTURA SEKOYA ALLISON STELLA; do
  aws batch submit-job --job-queue ${QUEUE} --job-definition ${DEF} \
    --job-name "recovery-${VARIETY,,}-$(date +%s)" \
    --container-overrides '{"command":["--varieties","'${VARIETY}'","--tuning","prod"]}'
done
```

### 8.3 Spot vs on-demand por preset

| Preset | Wallclock | P(interrupt) | Recomendacion |
|---|---|---|---|
| `smoke`    | ~1 min | ~0% | Spot |
| `dev`      | ~20 min | ~1-2% | Spot |
| `prod`     | ~1.5-2 h | ~5-10% | Spot + retry=2 |
| `prod_xl`  | ~5-6 h | ~20-30% | **on-demand** (queue `-queue-ondemand`) |

```bash
# Para forzar on-demand (prod_xl):
QUEUE_OD=$(terraform -chdir=envs/prod output -raw batch_job_queue_od)
aws batch submit-job --job-queue ${QUEUE_OD} --job-definition ${DEF} \
  --job-name "xl-pop-$(date +%s)" \
  --container-overrides '{"command":["--varieties","POP","--tuning","prod_xl"]}'
```

### 8.4 Rollback de imagen

```bash
# Listar tags
aws ecr describe-images --repository-name ml-training \
  --query 'reverse(sort_by(imageDetails,& imagePushedAt))[*].[imageTags[0],imagePushedAt]' \
  --output table

# Apuntar la job-def a un tag anterior via Terraform
cd envs/prod
terraform apply -var="trainer_image_tag=abc1234"
```

### 8.5 Bajar todo (ahorrar costos)

```bash
# Opcion A: scale-to-0 sin destruir (manual)
aws ecs update-service --cluster ml-training-cluster --service ml-training-mlflow --desired-count 0
aws rds stop-db-instance --db-instance-identifier ml-training-mlflow

# Opcion B: destruir todo (idempotente)
cd envs/prod
terraform destroy
# (los buckets S3 con data quedan; force_destroy_buckets=false por default)
```

### 8.6 Distinguir interrupcion de Spot vs fallo del codigo

```bash
aws batch list-jobs --job-queue ml-training-queue --job-status FAILED \
  --max-results 10 --query 'jobSummaryList[*].[jobName,jobId,statusReason]' \
  --output table
# statusReason "Host EC2 (instance ...) terminated" -> Spot interrupt
# statusReason con exit code != 0 -> fallo del codigo
```

---

## 9. Costos estimados (mensual, region us-east-1, 1 variedad/dia)

| Servicio | Recurso | Costo aprox |
|---|---|---|
| AWS Batch (Spot) | c6i.2xlarge × 6 jobs/mes × 1.5h | ~$1.20 |
| AWS Batch (Spot) | overhead (provision, idle) | ~$2 |
| ECS Fargate (MLflow) | 0.5 vCPU / 1 GB, 24/7 | ~$15 |
| RDS Postgres | db.t4g.micro, 20 GB gp3 | ~$15 |
| ALB | 1 ALB compartido | ~$18 |
| NAT Gateway | 1 NAT, single-AZ | ~$32 |
| S3 + ECR | data + artifacts + 2 repos | ~$2 |
| CloudWatch | Logs + alarms + metrics | ~$3 |
| Route 53 (opcional) | dominio MLflow | ~$0.50 |
| **Total** | | **~$88/mes** |

Optimizaciones disponibles:
- **NAT GW** es el item mas caro. Si no necesitas que el trainer hable con
  Internet (ya que pulla via VPC endpoints), podes eliminarlo y usar
  endpoints de S3, ECR, Logs, Secrets — ahorras ~$32/mes.
- **Bajar MLflow + RDS cuando no se usa** (§8.5): ahorras ~$30/mes los
  dias sin training.

---

## 10. Decisiones a revisar en el futuro

| Item | Hoy | Cuando reconsiderar |
|---|---|---|
| ALB publico (HTTP) | SG abierto a 0.0.0.0/0 + sin TLS | mover a HTTPS con ACM cuando tengas dominio (Route 53) |
| RDS db.t4g.micro single-AZ | 20 GB, sin replica | si tracking pasa de ~10k runs/mes o necesitas HA |
| NAT Gateway single-AZ | costo: ~$32/mes | reemplazar por VPC endpoints (S3 + ECR + CW) si querés ahorrar |
| Spot c6i.2xlarge | `prod` ~1.5-2 h, `prod_xl` ~5-6 h | escalar a `c6i.4xlarge` si necesitas `prod_xl` mas rapido |
| EMIT_CW_METRICS=1 | activado en job-def (alarma 12.2 operativa) | adoptar `aws-embedded-metrics` library |
| Single-region us-east-1 | sin DR | si requisito de DR, replicar state + buckets a otra region |
| Drift detection | no incluido | agregar Evidently/Whylogs scheduled job |
| MLflow self-hosted | mantener compat | considerar SageMaker MLflow managed cuando GA en tu region |

---

## 11. Apendice — checklist de implementacion

- [ ] **Bootstrap** §2: bucket de state, DynamoDB lock, SLRs de Spot/Batch
- [ ] **Build & push imagen MLflow custom** (psycopg2 + boto3) a ECR — necesaria antes del primer apply
- [ ] **Crear repo `ml-training-infra`** con la estructura §1
- [ ] **Llenar `terraform.tfvars`** (alert_email, mlflow_image)
- [ ] **`terraform init`** apuntando al backend del paso 1
- [ ] **`terraform apply`** — toma ~10-15 min la primera vez (RDS y ALB son lentos)
- [ ] **Confirmar suscripcion email** del SNS desde el inbox
- [ ] **Push primera imagen del trainer** a ECR (via GH Actions §7 o manual)
- [ ] **Smoke test** §8.1 con `--tuning smoke`
- [ ] **Subir Excel de prueba** al bucket `data/incoming/` y verificar que el dispatcher dispara un job
- [ ] **Verificar alarmas** disparando un job que falle a proposito (`docker run` con exit 1)

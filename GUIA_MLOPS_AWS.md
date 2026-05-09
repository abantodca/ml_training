# GUIA MLOPS AWS — ml-training

Implementacion paso a paso de la arquitectura MLOps en AWS para el pipeline
`ml_training` (regresion de productividad agricola, multi-variedad, batch).

Este documento es **copy-paste secuencial**: cada bloque es ejecutable desde
una shell con AWS CLI v2 ya configurada (`aws configure`). Los bloques
exportan variables de entorno que reutilizan los siguientes — corre la
sesion entera en una misma terminal o repite el bloque 1 al inicio.

---

## 0. Arquitectura objetivo

```
                  ┌─────────────────┐         ┌──────────────────┐
                  │  Desarrollador  │         │  Excel mensual   │
                  │  (push a main)  │         │  (upload manual) │
                  └────────┬────────┘         └─────────┬───────┘
                           │                            │
                           ▼                            ▼
                  ┌──────────────────┐        ┌──────────────────┐
                  │  GitHub          │        │  S3: data-raw/   │
                  │  + Actions       │        │  *.xlsx          │
                  └────────┬─────────┘        └─────────┬────────┘
                           │ build+push                 │ S3 PutObject
                           ▼                            ▼
                  ┌──────────────────┐        ┌──────────────────┐
                  │  ECR             │        │  EventBridge bus │◄───┐
                  │  ml-training:tag │        │  + Schedule cron │    │
                  └────────┬─────────┘        └─────────┬────────┘    │
                           │                            │             │
                           │      ┌────────────────────┘              │
                           │      ▼                                   │
                           │  ┌────────────┐                          │
                           │  │  Lambda    │  resuelve --varieties    │
                           │  │ dispatcher │  desde el evento         │
                           │  └─────┬──────┘                          │
                           │        │ batch:SubmitJob                 │
                           ▼        ▼                                 │
                ┌─────────────────────────────────────────┐           │
                │  AWS Batch                              │           │
                │  Compute Env: EC2 Spot c6i.2xlarge      │           │
                │  Job Queue:   ml-training-queue         │           │
                │  Job Def:     ml-training:N (image,cmd) │           │
                │                                         │           │
                │  python main.py --varieties POP,VENTURA │           │
                │                 --tuning prod           │           │
                └────────────┬────────────────────────────┘           │
                             │                                        │
            ┌────────────────┼─────────────────┐                      │
            │                │                 │                      │
            ▼                ▼                 ▼                      │
     ┌──────────────┐ ┌──────────────┐ ┌──────────────┐               │
     │  S3          │ │  MLflow      │ │ CloudWatch   │ ──── al       │
     │  artifacts/  │ │  (Fargate)   │ │  Logs        │   terminar    │
     │  reports/    │ │   ↑          │ │  + alarms    │   o fallar    │
     └──────────────┘ │   │          │ └──────┬───────┘               │
                      │   ▼          │        │                       │
                      │ RDS Postgres │        │ EventBridge job       │
                      │ (registry)   │        │ state-change event   ─┘
                      └──────────────┘        │
                                              ▼
                                       ┌─────────────┐
                                       │ Lambda      │
                                       │ notifier    │ ──▶ SNS / Slack
                                       └─────────────┘
```

### Compatibilidad Python 3.13 (todo el stack)

El proyecto corre **Python 3.13** end-to-end. Tabla de versiones por componente:

| Componente | Runtime | Cómo se fija |
|---|---|---|
| Local (Windows/Linux/Mac) | Python 3.13.9 | `python --version` (anaconda/pyenv) |
| Container del trainer (local + AWS Batch) | `python:3.13-slim` | `Dockerfile` ARG `PYTHON_VERSION=3.13-slim` |
| Lambda dispatcher | `python3.13` | Argumento `--runtime python3.13` en `aws lambda create-function` (§9.2) |
| Lambda notifier | `python3.13` | Idem en §11.2 |
| MLflow server (Fargate) | imagen oficial `ghcr.io/mlflow/mlflow:v3.12.0` | Self-contained; no depende del Python local. La compatibilidad cliente-servidor es por **versión de MLflow** (3.x ↔ 3.x), no por Python |
| MLflow client (en trainer) | `mlflow==3.12.0` (alineado con el server) | `requirements.txt` |
| GitHub Actions | El workflow solo hace `docker buildx`; el Python lo provee la imagen | `Dockerfile` |

**Verificación rápida del stack local:**

```bash
# 1) Python local
python --version    # debe imprimir 3.13.x

# 2) Reinstalar deps (despues del bump a mlflow 3.12.0)
pip install -r requirements.txt --upgrade

# 3) Confirmar que mlflow client coincide con la imagen del server
python -c "import mlflow; print(mlflow.__version__)"   # debe ser 3.12.0

# 4) Smoke import
python -c "import pandas, numpy, sklearn, lightgbm, xgboost, optuna, mlflow, plotly, statsmodels, boto3; print('OK')"

# 5) Build + run del container con Python 3.13
docker build -t ml-training:smoke .
docker run --rm ml-training:smoke python --version    # debe imprimir 3.13.x
```

**Compatibilidad de cada dependencia con Python 3.13:**

| Paquete | Versión | Soporta 3.13 desde |
|---|---|---|
| pandas | 2.2.3 | 2.2.3 (oficial) |
| numpy | 2.2.3 | 2.1.0 |
| scikit-learn | 1.6.1 | 1.6.0 |
| lightgbm | 4.5.0 | 4.5.0 |
| xgboost | 3.2.0 | 3.0.0 |
| optuna | 4.8.0 | 4.0.0 |
| mlflow | 3.12.0 | 3.0.0 |
| matplotlib | 3.10.6 | 3.10.0 |
| plotly | 6.3.0 | 5.x (pure Python) |
| joblib | 1.5.2 | 1.4.0 |
| boto3 | 1.38.0 | 1.35.x |
| statsmodels | 0.14.4 | 0.14.4 |
| openpyxl | 3.1.5 | siempre (pure Python) |
| patsy | 1.0.1 | siempre (pure Python) |
| scipy (dev) | 1.16.3 | 1.14.0 |

**Costos: nada cambia por usar 3.13** (Lambda 3.13 cuesta lo mismo que 3.12; la imagen `python:3.13-slim` no es más cara que `3.12-slim`).

---

### Costos estimados (mensual, region us-east-1)

| Servicio | Recurso | Costo aprox |
|---|---|---|
| AWS Batch | EC2 Spot c6i.2xlarge × ~30h/mes | ~$8 |
| ECS Fargate | MLflow 0.5 vCPU / 1 GB, 24/7 | ~$15 |
| RDS Postgres | db.t4g.micro, 20 GB gp3 | ~$15 |
| ALB (MLflow UI) | 1 ALB compartido | ~$18 |
| S3 | ~5 GB artifacts + requests | ~$1 |
| CloudWatch | Logs + 5 alarms + metrics | ~$3 |
| Lambda | dispatcher + notifier (<1k invoc/mes) | ~$0 |
| EventBridge | <1k events/mes | ~$0 |
| Secrets Manager | 1 secret | ~$0.40 |
| ECR | <1 GB storage | ~$0.10 |
| **Total** | | **~$60/mes** |

NOTAS de optimizacion:
- Si quitas el ALB y accedes a MLflow vía VPN/SSM port-forward: ahorras **~$18/mes**.
- Si usas Aurora Serverless v2 con auto-scale-to-0 en lugar de RDS db.t4g.micro:
  costo cae a ~$1-3/mes cuando idle (la mayoria del tiempo).
- AWS Batch con Spot c6i.2xlarge cuesta ~$0.13/hora (vs $0.34 on-demand). Para
  un training de 40min mensual, esto es trivial.

---

## 1. Variables base (correr en CADA sesion)

```bash
# ============================================================
# Variables de la sesion. Editar AWS_REGION si quieres otra.
# ============================================================
export AWS_REGION=us-east-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

export PROJECT=ml-training
export S3_DATA_BUCKET=${PROJECT}-data-raw-${AWS_ACCOUNT_ID}
export S3_ARTIFACTS_BUCKET=cabanto-ml-artifacts          # ya existe; reusamos
export S3_MLFLOW_BUCKET=${PROJECT}-mlflow-${AWS_ACCOUNT_ID}

export ECR_REPO=${PROJECT}
export ECR_URI=${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}

export VPC_NAME=${PROJECT}-vpc
export DB_NAME=mlflow
export DB_USER=mlflow

echo "Account=${AWS_ACCOUNT_ID} Region=${AWS_REGION}"
```

---

## 2. Networking — VPC, subnets, NAT, security groups

```bash
# ============================================================
# VPC con 2 subnets publicas (NAT, ALB) + 2 privadas (Batch, RDS, Fargate)
# ============================================================
VPC_ID=$(aws ec2 create-vpc \
  --cidr-block 10.20.0.0/16 \
  --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=${VPC_NAME}}]" \
  --query Vpc.VpcId --output text --region ${AWS_REGION})
aws ec2 modify-vpc-attribute --vpc-id ${VPC_ID} --enable-dns-hostnames
echo "VPC_ID=${VPC_ID}"

# Internet Gateway
IGW_ID=$(aws ec2 create-internet-gateway \
  --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=${VPC_NAME}-igw}]" \
  --query InternetGateway.InternetGatewayId --output text)
aws ec2 attach-internet-gateway --vpc-id ${VPC_ID} --internet-gateway-id ${IGW_ID}

# 2 AZ
AZ_A=$(aws ec2 describe-availability-zones --query 'AvailabilityZones[0].ZoneName' --output text)
AZ_B=$(aws ec2 describe-availability-zones --query 'AvailabilityZones[1].ZoneName' --output text)

# Subnets publicas (para NAT y ALB)
SUBNET_PUB_A=$(aws ec2 create-subnet --vpc-id ${VPC_ID} --availability-zone ${AZ_A} \
  --cidr-block 10.20.0.0/24 \
  --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${VPC_NAME}-pub-a}]" \
  --query Subnet.SubnetId --output text)
SUBNET_PUB_B=$(aws ec2 create-subnet --vpc-id ${VPC_ID} --availability-zone ${AZ_B} \
  --cidr-block 10.20.1.0/24 \
  --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${VPC_NAME}-pub-b}]" \
  --query Subnet.SubnetId --output text)
aws ec2 modify-subnet-attribute --subnet-id ${SUBNET_PUB_A} --map-public-ip-on-launch
aws ec2 modify-subnet-attribute --subnet-id ${SUBNET_PUB_B} --map-public-ip-on-launch

# Subnets privadas (para Batch compute, RDS, Fargate task)
SUBNET_PRV_A=$(aws ec2 create-subnet --vpc-id ${VPC_ID} --availability-zone ${AZ_A} \
  --cidr-block 10.20.10.0/24 \
  --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${VPC_NAME}-prv-a}]" \
  --query Subnet.SubnetId --output text)
SUBNET_PRV_B=$(aws ec2 create-subnet --vpc-id ${VPC_ID} --availability-zone ${AZ_B} \
  --cidr-block 10.20.11.0/24 \
  --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${VPC_NAME}-prv-b}]" \
  --query Subnet.SubnetId --output text)

# Route table publica
RT_PUB=$(aws ec2 create-route-table --vpc-id ${VPC_ID} \
  --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=${VPC_NAME}-rt-pub}]" \
  --query RouteTable.RouteTableId --output text)
aws ec2 create-route --route-table-id ${RT_PUB} --destination-cidr-block 0.0.0.0/0 --gateway-id ${IGW_ID}
aws ec2 associate-route-table --subnet-id ${SUBNET_PUB_A} --route-table-id ${RT_PUB}
aws ec2 associate-route-table --subnet-id ${SUBNET_PUB_B} --route-table-id ${RT_PUB}

# NAT Gateway en SUBNET_PUB_A
EIP_ALLOC=$(aws ec2 allocate-address --domain vpc --query AllocationId --output text)
NAT_ID=$(aws ec2 create-nat-gateway --subnet-id ${SUBNET_PUB_A} --allocation-id ${EIP_ALLOC} \
  --tag-specifications "ResourceType=natgateway,Tags=[{Key=Name,Value=${VPC_NAME}-nat}]" \
  --query NatGateway.NatGatewayId --output text)
echo "Esperando NAT..."
aws ec2 wait nat-gateway-available --nat-gateway-ids ${NAT_ID}

# Route table privada (sale por NAT)
RT_PRV=$(aws ec2 create-route-table --vpc-id ${VPC_ID} \
  --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=${VPC_NAME}-rt-prv}]" \
  --query RouteTable.RouteTableId --output text)
aws ec2 create-route --route-table-id ${RT_PRV} --destination-cidr-block 0.0.0.0/0 --nat-gateway-id ${NAT_ID}
aws ec2 associate-route-table --subnet-id ${SUBNET_PRV_A} --route-table-id ${RT_PRV}
aws ec2 associate-route-table --subnet-id ${SUBNET_PRV_B} --route-table-id ${RT_PRV}

# Persistir IDs para sesiones siguientes
cat > .aws_ids.env <<EOF
export VPC_ID=${VPC_ID}
export SUBNET_PUB_A=${SUBNET_PUB_A}
export SUBNET_PUB_B=${SUBNET_PUB_B}
export SUBNET_PRV_A=${SUBNET_PRV_A}
export SUBNET_PRV_B=${SUBNET_PRV_B}
EOF
echo "IDs guardados en .aws_ids.env -- 'source .aws_ids.env' al volver a empezar."
```

### Security groups

```bash
source .aws_ids.env

# SG para RDS Postgres (acepta solo trafico de Batch y Fargate)
SG_RDS=$(aws ec2 create-security-group --group-name ${PROJECT}-rds \
  --description "RDS Postgres for MLflow" --vpc-id ${VPC_ID} \
  --query GroupId --output text)

# SG para Fargate (MLflow server)
SG_MLFLOW=$(aws ec2 create-security-group --group-name ${PROJECT}-mlflow \
  --description "MLflow server (Fargate)" --vpc-id ${VPC_ID} \
  --query GroupId --output text)

# SG para Batch jobs (acceden a RDS y a internet via NAT)
SG_BATCH=$(aws ec2 create-security-group --group-name ${PROJECT}-batch \
  --description "Batch jobs" --vpc-id ${VPC_ID} \
  --query GroupId --output text)

# SG para ALB (acepta 80/443 desde tu IP)
SG_ALB=$(aws ec2 create-security-group --group-name ${PROJECT}-alb \
  --description "ALB for MLflow UI" --vpc-id ${VPC_ID} \
  --query GroupId --output text)

# Reglas
MY_IP=$(curl -s https://checkip.amazonaws.com)/32
aws ec2 authorize-security-group-ingress --group-id ${SG_ALB} \
  --protocol tcp --port 80 --cidr ${MY_IP}
# RDS acepta de Fargate y Batch
aws ec2 authorize-security-group-ingress --group-id ${SG_RDS} \
  --protocol tcp --port 5432 --source-group ${SG_MLFLOW}
aws ec2 authorize-security-group-ingress --group-id ${SG_RDS} \
  --protocol tcp --port 5432 --source-group ${SG_BATCH}
# MLflow acepta de ALB y de Batch (Batch llama directo a la API)
aws ec2 authorize-security-group-ingress --group-id ${SG_MLFLOW} \
  --protocol tcp --port 5000 --source-group ${SG_ALB}
aws ec2 authorize-security-group-ingress --group-id ${SG_MLFLOW} \
  --protocol tcp --port 5000 --source-group ${SG_BATCH}

cat >> .aws_ids.env <<EOF
export SG_RDS=${SG_RDS}
export SG_MLFLOW=${SG_MLFLOW}
export SG_BATCH=${SG_BATCH}
export SG_ALB=${SG_ALB}
EOF
echo "Security groups creados."
```

---

## 3. S3 buckets

```bash
source .aws_ids.env

# Bucket para data raw (Excel mensual subido por usuario de negocio)
aws s3api create-bucket --bucket ${S3_DATA_BUCKET} --region ${AWS_REGION}
aws s3api put-bucket-versioning --bucket ${S3_DATA_BUCKET} \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket ${S3_DATA_BUCKET} \
  --server-side-encryption-configuration '{
    "Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# Bucket para MLflow artifacts (separado del de pipelines/reportes)
aws s3api create-bucket --bucket ${S3_MLFLOW_BUCKET} --region ${AWS_REGION}
aws s3api put-bucket-versioning --bucket ${S3_MLFLOW_BUCKET} \
  --versioning-configuration Status=Enabled

# El bucket ${S3_ARTIFACTS_BUCKET} (cabanto-ml-artifacts) ya existe. Verificar:
aws s3 ls s3://${S3_ARTIFACTS_BUCKET}/ | head

# Habilitar EventBridge en data-raw (para que un PutObject genere evento)
aws s3api put-bucket-notification-configuration \
  --bucket ${S3_DATA_BUCKET} \
  --notification-configuration '{"EventBridgeConfiguration":{}}'
```

---

## 4. ECR — repositorio + push de imagen

```bash
source .aws_ids.env

aws ecr create-repository --repository-name ${ECR_REPO} \
  --image-scanning-configuration scanOnPush=true \
  --region ${AWS_REGION}

# Login docker -> ECR
aws ecr get-login-password --region ${AWS_REGION} \
  | docker login --username AWS --password-stdin ${ECR_URI}

# Build con build-args (los toma el Dockerfile actual)
GIT_SHA=$(git rev-parse --short HEAD)
BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker buildx build \
  --platform linux/amd64 \
  --build-arg GIT_SHA=${GIT_SHA} \
  --build-arg BUILD_DATE=${BUILD_DATE} \
  --build-arg VERSION=v0.1.0 \
  -t ${ECR_URI}:${GIT_SHA} \
  -t ${ECR_URI}:latest \
  --push .

echo "Imagen pushed: ${ECR_URI}:${GIT_SHA}"
```

---

## 5. Secrets Manager — password de la DB

```bash
DB_PASSWORD=$(aws secretsmanager get-random-password \
  --password-length 32 --exclude-characters '"@/\$%' \
  --query RandomPassword --output text)

DB_SECRET_ARN=$(aws secretsmanager create-secret \
  --name ${PROJECT}/mlflow/db \
  --description "MLflow Postgres credentials" \
  --secret-string "{\"username\":\"${DB_USER}\",\"password\":\"${DB_PASSWORD}\",\"dbname\":\"${DB_NAME}\"}" \
  --query ARN --output text)

cat >> .aws_ids.env <<EOF
export DB_SECRET_ARN=${DB_SECRET_ARN}
EOF
echo "Secret creado: ${DB_SECRET_ARN}"
```

---

## 6. RDS Postgres — backend de MLflow

```bash
source .aws_ids.env

# DB subnet group sobre las 2 subnets privadas
aws rds create-db-subnet-group \
  --db-subnet-group-name ${PROJECT}-db-subnets \
  --db-subnet-group-description "MLflow DB subnets" \
  --subnet-ids ${SUBNET_PRV_A} ${SUBNET_PRV_B}

# Recuperar password del secret
DB_PASSWORD=$(aws secretsmanager get-secret-value --secret-id ${DB_SECRET_ARN} \
  --query SecretString --output text | jq -r .password)

aws rds create-db-instance \
  --db-instance-identifier ${PROJECT}-mlflow \
  --engine postgres \
  --engine-version 15.7 \
  --db-instance-class db.t4g.micro \
  --allocated-storage 20 \
  --storage-type gp3 \
  --master-username ${DB_USER} \
  --master-user-password "${DB_PASSWORD}" \
  --db-name ${DB_NAME} \
  --vpc-security-group-ids ${SG_RDS} \
  --db-subnet-group-name ${PROJECT}-db-subnets \
  --backup-retention-period 7 \
  --no-publicly-accessible \
  --storage-encrypted

echo "Esperando RDS (5-10 min)..."
aws rds wait db-instance-available --db-instance-identifier ${PROJECT}-mlflow

DB_ENDPOINT=$(aws rds describe-db-instances \
  --db-instance-identifier ${PROJECT}-mlflow \
  --query 'DBInstances[0].Endpoint.Address' --output text)

cat >> .aws_ids.env <<EOF
export DB_ENDPOINT=${DB_ENDPOINT}
EOF
echo "RDS endpoint: ${DB_ENDPOINT}"
```

---

## 7. MLflow en ECS Fargate

### 7.1 Crear cluster + IAM roles

```bash
source .aws_ids.env

aws ecs create-cluster --cluster-name ${PROJECT}-cluster

# Trust policy ECS task
cat > /tmp/ecs-trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON

# Execution role (pull ECR, escribir Logs, leer Secret)
aws iam create-role --role-name ${PROJECT}-ecs-exec \
  --assume-role-policy-document file:///tmp/ecs-trust.json
aws iam attach-role-policy --role-name ${PROJECT}-ecs-exec \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

# Permiso extra para leer el secret de la DB
aws iam put-role-policy --role-name ${PROJECT}-ecs-exec \
  --policy-name read-db-secret --policy-document '{
    "Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Action":["secretsmanager:GetSecretValue"],
    "Resource":"'${DB_SECRET_ARN}'"}]}'

# Task role (la app accede a S3 para artifacts)
aws iam create-role --role-name ${PROJECT}-mlflow-task \
  --assume-role-policy-document file:///tmp/ecs-trust.json
aws iam put-role-policy --role-name ${PROJECT}-mlflow-task \
  --policy-name s3-mlflow --policy-document '{
    "Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket"],
    "Resource":["arn:aws:s3:::'${S3_MLFLOW_BUCKET}'",
                "arn:aws:s3:::'${S3_MLFLOW_BUCKET}'/*"]}]}'

ECS_EXEC_ARN=arn:aws:iam::${AWS_ACCOUNT_ID}:role/${PROJECT}-ecs-exec
MLFLOW_TASK_ARN=arn:aws:iam::${AWS_ACCOUNT_ID}:role/${PROJECT}-mlflow-task
echo "Roles creados."
```

### 7.2 Task definition + service

```bash
# Log group
aws logs create-log-group --log-group-name /ecs/${PROJECT}-mlflow || true

# Task definition (MLflow oficial v3.12.0, mismo pin que tu compose)
cat > /tmp/mlflow-task.json <<JSON
{
  "family": "${PROJECT}-mlflow",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "executionRoleArn": "${ECS_EXEC_ARN}",
  "taskRoleArn": "${MLFLOW_TASK_ARN}",
  "containerDefinitions": [{
    "name": "mlflow",
    "image": "ghcr.io/mlflow/mlflow:v3.12.0",
    "essential": true,
    "portMappings": [{"containerPort": 5000, "protocol": "tcp"}],
    "secrets": [
      {"name":"DB_USER",     "valueFrom":"${DB_SECRET_ARN}:username::"},
      {"name":"DB_PASSWORD", "valueFrom":"${DB_SECRET_ARN}:password::"},
      {"name":"DB_NAME",     "valueFrom":"${DB_SECRET_ARN}:dbname::"}
    ],
    "environment": [
      {"name":"DB_HOST","value":"${DB_ENDPOINT}"},
      {"name":"AWS_DEFAULT_REGION","value":"${AWS_REGION}"}
    ],
    "command": ["sh","-c",
      "pip install psycopg2-binary boto3 && mlflow server --host 0.0.0.0 --port 5000 --backend-store-uri postgresql://\$DB_USER:\$DB_PASSWORD@\$DB_HOST:5432/\$DB_NAME --default-artifact-root s3://${S3_MLFLOW_BUCKET}/artifacts"
    ],
    "logConfiguration": {
      "logDriver":"awslogs",
      "options":{
        "awslogs-group":"/ecs/${PROJECT}-mlflow",
        "awslogs-region":"${AWS_REGION}",
        "awslogs-stream-prefix":"mlflow"
      }
    }
  }]
}
JSON

aws ecs register-task-definition --cli-input-json file:///tmp/mlflow-task.json
```

### 7.3 ALB + target group + listener

```bash
# ALB en subnets publicas
ALB_ARN=$(aws elbv2 create-load-balancer \
  --name ${PROJECT}-alb \
  --subnets ${SUBNET_PUB_A} ${SUBNET_PUB_B} \
  --security-groups ${SG_ALB} \
  --scheme internet-facing --type application \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)

TG_ARN=$(aws elbv2 create-target-group \
  --name ${PROJECT}-mlflow-tg \
  --protocol HTTP --port 5000 \
  --vpc-id ${VPC_ID} \
  --target-type ip \
  --health-check-path /health \
  --health-check-interval-seconds 30 \
  --query 'TargetGroups[0].TargetGroupArn' --output text)

aws elbv2 create-listener --load-balancer-arn ${ALB_ARN} \
  --protocol HTTP --port 80 \
  --default-actions Type=forward,TargetGroupArn=${TG_ARN}

ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns ${ALB_ARN} \
  --query 'LoadBalancers[0].DNSName' --output text)

cat >> .aws_ids.env <<EOF
export ALB_ARN=${ALB_ARN}
export TG_ARN=${TG_ARN}
export ALB_DNS=${ALB_DNS}
EOF
echo "MLflow UI estara en: http://${ALB_DNS}"
```

### 7.4 Service ECS

```bash
aws ecs create-service \
  --cluster ${PROJECT}-cluster \
  --service-name ${PROJECT}-mlflow \
  --task-definition ${PROJECT}-mlflow \
  --desired-count 1 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNET_PRV_A},${SUBNET_PRV_B}],securityGroups=[${SG_MLFLOW}],assignPublicIp=DISABLED}" \
  --load-balancers "targetGroupArn=${TG_ARN},containerName=mlflow,containerPort=5000"

echo "Esperando que el service llegue a steady state (~3 min)..."
aws ecs wait services-stable --cluster ${PROJECT}-cluster --services ${PROJECT}-mlflow

# Internal DNS para que Batch llame a MLflow sin pasar por ALB
MLFLOW_INTERNAL_URI="http://${ALB_DNS}"  # via ALB tambien funciona desde Batch
echo "MLFLOW_TRACKING_URI = ${MLFLOW_INTERNAL_URI}"
```

NOTA: Para que Batch llame a MLflow vía DNS interno (sin pasar por ALB
público), podés crear un ALB interno separado o usar Service Connect /
Cloud Map. Para arrancar, el ALB público restringido por SG (solo desde
SG_BATCH) ya alcanza.

---

## 8. AWS Batch — compute env, queue, job definition

### 8.1 IAM roles para Batch

```bash
source .aws_ids.env

# Trust policy para EC2
cat > /tmp/ec2-trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON

# Trust policy para ECS (job container role)
cat > /tmp/ecs-tasks-trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON

# Instance role (EC2 host de Batch)
aws iam create-role --role-name ${PROJECT}-batch-instance \
  --assume-role-policy-document file:///tmp/ec2-trust.json
aws iam attach-role-policy --role-name ${PROJECT}-batch-instance \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role
aws iam create-instance-profile --instance-profile-name ${PROJECT}-batch-instance
aws iam add-role-to-instance-profile --instance-profile-name ${PROJECT}-batch-instance \
  --role-name ${PROJECT}-batch-instance

# Job role (lo que el container puede hacer: S3 + MLflow no requiere IAM)
aws iam create-role --role-name ${PROJECT}-batch-job \
  --assume-role-policy-document file:///tmp/ecs-tasks-trust.json
aws iam put-role-policy --role-name ${PROJECT}-batch-job \
  --policy-name s3-rw --policy-document '{
    "Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Action":["s3:GetObject","s3:PutObject","s3:ListBucket"],
    "Resource":["arn:aws:s3:::'${S3_DATA_BUCKET}'","arn:aws:s3:::'${S3_DATA_BUCKET}'/*",
                "arn:aws:s3:::'${S3_ARTIFACTS_BUCKET}'","arn:aws:s3:::'${S3_ARTIFACTS_BUCKET}'/*",
                "arn:aws:s3:::'${S3_MLFLOW_BUCKET}'","arn:aws:s3:::'${S3_MLFLOW_BUCKET}'/*"]}]}'

# Execution role (pull ECR, escribir CW Logs)
aws iam create-role --role-name ${PROJECT}-batch-exec \
  --assume-role-policy-document file:///tmp/ecs-tasks-trust.json
aws iam attach-role-policy --role-name ${PROJECT}-batch-exec \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

# Service-linked role para Batch (idempotente)
aws iam create-service-linked-role --aws-service-name batch.amazonaws.com 2>/dev/null || true
```

### 8.2 Compute environment (Spot c6i.2xlarge)

```bash
BATCH_INSTANCE_PROFILE_ARN=arn:aws:iam::${AWS_ACCOUNT_ID}:instance-profile/${PROJECT}-batch-instance

aws batch create-compute-environment \
  --compute-environment-name ${PROJECT}-ce \
  --type MANAGED \
  --state ENABLED \
  --compute-resources "type=EC2,allocationStrategy=BEST_FIT_PROGRESSIVE,minvCpus=0,maxvCpus=32,desiredvCpus=0,instanceTypes=c6i.2xlarge,subnets=${SUBNET_PRV_A},${SUBNET_PRV_B},securityGroupIds=${SG_BATCH},instanceRole=${BATCH_INSTANCE_PROFILE_ARN},bidPercentage=70,spotIamFleetRole=arn:aws:iam::${AWS_ACCOUNT_ID}:role/aws-service-role/spotfleet.amazonaws.com/AWSServiceRoleForEC2SpotFleet"

echo "Esperando compute env VALID..."
sleep 20

aws batch create-job-queue \
  --job-queue-name ${PROJECT}-queue \
  --priority 1 --state ENABLED \
  --compute-environment-order order=1,computeEnvironment=${PROJECT}-ce
```

NOTA: si tu cuenta no tiene `AWSServiceRoleForEC2SpotFleet` creado,
ejecutalo con:
```bash
aws iam create-service-linked-role --aws-service-name spotfleet.amazonaws.com
```

### 8.3 Job definition

El comando del container respeta el `ENTRYPOINT` del Dockerfile actual:
`["/usr/bin/tini","--","python","main.py"]`. Pasamos solo los args.

```bash
aws logs create-log-group --log-group-name /aws/batch/${PROJECT} || true

BATCH_EXEC_ARN=arn:aws:iam::${AWS_ACCOUNT_ID}:role/${PROJECT}-batch-exec
BATCH_JOB_ARN=arn:aws:iam::${AWS_ACCOUNT_ID}:role/${PROJECT}-batch-job

cat > /tmp/job-def.json <<JSON
{
  "jobDefinitionName": "${PROJECT}",
  "type": "container",
  "platformCapabilities": ["EC2"],
  "containerProperties": {
    "image": "${ECR_URI}:latest",
    "command": ["--varieties","POP","--tuning","prod"],
    "executionRoleArn": "${BATCH_EXEC_ARN}",
    "jobRoleArn": "${BATCH_JOB_ARN}",
    "resourceRequirements": [
      {"type":"VCPU","value":"8"},
      {"type":"MEMORY","value":"15000"}
    ],
    "environment": [
      {"name":"MLFLOW_TRACKING_URI","value":"http://${ALB_DNS}"},
      {"name":"S3_ARTIFACTS_BUCKET","value":"${S3_ARTIFACTS_BUCKET}"},
      {"name":"S3_ARTIFACTS_PREFIX","value":"artifacts"},
      {"name":"S3_REPORTS_PREFIX","value":"reports"},
      {"name":"AWS_DEFAULT_REGION","value":"${AWS_REGION}"}
    ],
    "logConfiguration": {
      "logDriver":"awslogs",
      "options":{
        "awslogs-group":"/aws/batch/${PROJECT}",
        "awslogs-region":"${AWS_REGION}",
        "awslogs-stream-prefix":"job"
      }
    }
  },
  "retryStrategy": { "attempts": 2 },
  "timeout": { "attemptDurationSeconds": 7200 }
}
JSON

aws batch register-job-definition --cli-input-json file:///tmp/job-def.json
echo "Job definition registrada (revision 1)."
```

### 8.4 Test manual del job

```bash
JOB_ID=$(aws batch submit-job \
  --job-name smoke-$(date +%s) \
  --job-queue ${PROJECT}-queue \
  --job-definition ${PROJECT} \
  --container-overrides '{"command":["--varieties","POP","--tuning","smoke"]}' \
  --query jobId --output text)

echo "Job lanzado: ${JOB_ID}"
echo "Logs: aws logs tail /aws/batch/${PROJECT} --follow"
```

---

## 9. Lambda dispatcher

Recibe eventos de EventBridge (cron + S3 PutObject) y resuelve qué
varieties entrenar; submite el job a Batch.

### 9.1 Codigo

Crear `aws/lambda/dispatcher.py` localmente:

```python
"""Lambda dispatcher: EventBridge -> AWS Batch SubmitJob.

El usuario de negocio sube SOLO BD_HISTORICO_ACUMULADO.xlsx al bucket
data-raw. El Lambda extrae bucket+key del evento y los pasa al job de Batch
como env vars; main.py los usa en `_hydrate_data_from_s3()` para descargar
el acumulado y correr scripts.prepare_data.split_workbook -> DB-HISTORICA.

Eventos soportados:
1. Schedule cron (EventBridge Schedule):
   detail = {"varieties":"all","tuning":"prod",
             "s3_data_bucket":"...", "s3_data_key":"latest/BD_HISTORICO_ACUMULADO.xlsx"}
2. S3 PutObject (data-raw):
   detail.bucket.name = "ml-training-data-raw-..."
   detail.object.key  = "incoming/2026-05/BD_HISTORICO_ACUMULADO.xlsx"
   -> entrena 'all' variedades sobre ese acumulado.
"""
import os
import re

import boto3

batch = boto3.client("batch")
JOB_QUEUE = os.environ["JOB_QUEUE"]
JOB_DEFINITION = os.environ["JOB_DEFINITION"]
DEFAULT_TUNING = os.environ.get("DEFAULT_TUNING", "prod")
DEFAULT_BUCKET = os.environ.get("DEFAULT_DATA_BUCKET", "")
DEFAULT_KEY = os.environ.get("DEFAULT_DATA_KEY", "")


def _from_schedule(detail):
    return (
        detail.get("varieties", "all"),
        detail.get("tuning", DEFAULT_TUNING),
        detail.get("s3_data_bucket", DEFAULT_BUCKET),
        detail.get("s3_data_key", DEFAULT_KEY),
    )


def _from_s3(detail):
    bucket = detail["bucket"]["name"]
    key = detail["object"]["key"]
    return "all", DEFAULT_TUNING, bucket, key


def lambda_handler(event, context):
    source = event.get("source", "")
    detail = event.get("detail", {})

    if source == "aws.s3":
        varieties, tuning, s3_bucket, s3_key = _from_s3(detail)
    else:
        varieties, tuning, s3_bucket, s3_key = _from_schedule(detail)

    if not (s3_bucket and s3_key):
        raise ValueError(
            "No se pudo determinar S3_DATA_BUCKET/S3_DATA_KEY. "
            "Pasalos en detail o seteá DEFAULT_DATA_BUCKET/DEFAULT_DATA_KEY."
        )

    job_name = re.sub(r"[^a-zA-Z0-9_-]", "-",
                      f"train-{varieties}-{tuning}")[:128]

    resp = batch.submit_job(
        jobName=job_name,
        jobQueue=JOB_QUEUE,
        jobDefinition=JOB_DEFINITION,
        containerOverrides={
            "command": ["--varieties", varieties, "--tuning", tuning],
            "environment": [
                {"name": "S3_DATA_BUCKET", "value": s3_bucket},
                {"name": "S3_DATA_KEY",    "value": s3_key},
            ],
        },
    )
    return {
        "jobId": resp["jobId"],
        "varieties": varieties,
        "tuning": tuning,
        "s3_data": f"s3://{s3_bucket}/{s3_key}",
    }
```

### 9.2 Crear funcion

```bash
mkdir -p aws/lambda && cd aws/lambda
# (pega el codigo en dispatcher.py)
zip dispatcher.zip dispatcher.py
cd ../..

# Trust policy
cat > /tmp/lambda-trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON

aws iam create-role --role-name ${PROJECT}-dispatcher \
  --assume-role-policy-document file:///tmp/lambda-trust.json
aws iam attach-role-policy --role-name ${PROJECT}-dispatcher \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam put-role-policy --role-name ${PROJECT}-dispatcher \
  --policy-name batch-submit --policy-document '{
    "Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Action":["batch:SubmitJob"],
    "Resource":["arn:aws:batch:'${AWS_REGION}':'${AWS_ACCOUNT_ID}':job-queue/'${PROJECT}'-queue",
                "arn:aws:batch:'${AWS_REGION}':'${AWS_ACCOUNT_ID}':job-definition/'${PROJECT}'*"]}]}'

sleep 10  # propagacion IAM

aws lambda create-function \
  --function-name ${PROJECT}-dispatcher \
  --runtime python3.13 \
  --role arn:aws:iam::${AWS_ACCOUNT_ID}:role/${PROJECT}-dispatcher \
  --handler dispatcher.lambda_handler \
  --zip-file fileb://aws/lambda/dispatcher.zip \
  --environment "Variables={JOB_QUEUE=${PROJECT}-queue,JOB_DEFINITION=${PROJECT},DEFAULT_TUNING=prod,DEFAULT_DATA_BUCKET=${S3_DATA_BUCKET},DEFAULT_DATA_KEY=latest/BD_HISTORICO_ACUMULADO.xlsx}"
```

---

## 10. EventBridge — schedule cron + trigger S3

### 10.1 Schedule mensual

```bash
DISPATCHER_ARN=arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${PROJECT}-dispatcher

# Permiso para que EventBridge invoque al Lambda
aws lambda add-permission \
  --function-name ${PROJECT}-dispatcher \
  --statement-id eb-schedule \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com

# Regla: dia 5 de cada mes a las 06:00 UTC
aws events put-rule \
  --name ${PROJECT}-monthly-train \
  --schedule-expression "cron(0 6 5 * ? *)" \
  --state ENABLED

aws events put-targets --rule ${PROJECT}-monthly-train --targets '[{
  "Id":"1",
  "Arn":"'${DISPATCHER_ARN}'",
  "Input":"{\"detail\":{\"varieties\":\"all\",\"tuning\":\"prod\"}}"
}]'
```

### 10.2 Trigger por upload de Excel a S3

```bash
# Permiso EventBridge -> Lambda (otro statement-id)
aws lambda add-permission \
  --function-name ${PROJECT}-dispatcher \
  --statement-id eb-s3 \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com

# Regla que matchea PutObject en data-raw
aws events put-rule \
  --name ${PROJECT}-data-uploaded \
  --event-pattern '{
    "source":["aws.s3"],
    "detail-type":["Object Created"],
    "detail":{"bucket":{"name":["'${S3_DATA_BUCKET}'"]},
              "object":{"key":[{"prefix":"incoming/","suffix":"BD_HISTORICO_ACUMULADO.xlsx"}]}}
  }' \
  --state ENABLED

aws events put-targets --rule ${PROJECT}-data-uploaded --targets '[{
  "Id":"1","Arn":"'${DISPATCHER_ARN}'"
}]'
```

---

## 11. Lambda notifier — job state changes -> SNS / Slack

### 11.1 SNS topic

```bash
SNS_ARN=$(aws sns create-topic --name ${PROJECT}-alerts \
  --query TopicArn --output text)

# Suscribir email (revisa tu inbox para confirmar)
aws sns subscribe --topic-arn ${SNS_ARN} \
  --protocol email --notification-endpoint "tu-email@ejemplo.com"
```

### 11.2 Lambda notifier

`aws/lambda/notifier.py`:

```python
"""Notifica via SNS cuando un job de Batch cambia a SUCCEEDED o FAILED."""
import json
import os

import boto3

sns = boto3.client("sns")
TOPIC_ARN = os.environ["TOPIC_ARN"]


def lambda_handler(event, context):
    detail = event["detail"]
    status = detail["status"]
    if status not in ("SUCCEEDED", "FAILED"):
        return {"skipped": status}

    job_name = detail.get("jobName", "?")
    job_id = detail.get("jobId", "?")
    overrides = detail.get("container", {}).get("command", [])

    msg = (
        f"[ml-training] {status}\n"
        f"jobName={job_name}\njobId={job_id}\n"
        f"command={' '.join(overrides)}\n"
    )
    if status == "FAILED":
        reason = detail.get("statusReason", "(sin razon)")
        msg += f"reason={reason}\n"

    sns.publish(TopicArn=TOPIC_ARN, Subject=f"ml-training {status}", Message=msg)
    return {"published": True}
```

```bash
cd aws/lambda
zip notifier.zip notifier.py
cd ../..

aws iam create-role --role-name ${PROJECT}-notifier \
  --assume-role-policy-document file:///tmp/lambda-trust.json
aws iam attach-role-policy --role-name ${PROJECT}-notifier \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam put-role-policy --role-name ${PROJECT}-notifier \
  --policy-name sns-publish --policy-document '{
    "Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Action":["sns:Publish"],"Resource":"'${SNS_ARN}'"}]}'

sleep 10
aws lambda create-function \
  --function-name ${PROJECT}-notifier \
  --runtime python3.13 \
  --role arn:aws:iam::${AWS_ACCOUNT_ID}:role/${PROJECT}-notifier \
  --handler notifier.lambda_handler \
  --zip-file fileb://aws/lambda/notifier.zip \
  --environment "Variables={TOPIC_ARN=${SNS_ARN}}"

NOTIFIER_ARN=arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${PROJECT}-notifier

aws lambda add-permission \
  --function-name ${PROJECT}-notifier \
  --statement-id eb-batch \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com

# Regla EventBridge: jobs de nuestra queue que cambian de estado
aws events put-rule \
  --name ${PROJECT}-job-state \
  --event-pattern '{
    "source":["aws.batch"],
    "detail-type":["Batch Job State Change"],
    "detail":{"status":["SUCCEEDED","FAILED"],
              "jobQueue":[{"prefix":"arn:aws:batch:'${AWS_REGION}':'${AWS_ACCOUNT_ID}':job-queue/'${PROJECT}'"}]}
  }' \
  --state ENABLED

aws events put-targets --rule ${PROJECT}-job-state --targets '[{
  "Id":"1","Arn":"'${NOTIFIER_ARN}'"
}]'
```

### 11.3 (Opcional) Slack webhook

Reemplaza el `sns.publish` con un POST al webhook de Slack:

```python
import urllib.request, json
WEBHOOK = os.environ["SLACK_WEBHOOK"]
req = urllib.request.Request(
    WEBHOOK,
    data=json.dumps({"text": msg}).encode(),
    headers={"Content-Type": "application/json"},
)
urllib.request.urlopen(req, timeout=5)
```

Y agregale al Lambda `SLACK_WEBHOOK` como env var.

---

## 12. CloudWatch Alarms — calidad y operacion

### 12.1 Alarma: job failure

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name ${PROJECT}-job-failures \
  --alarm-description "Cualquier fallo de training en ultima hora" \
  --namespace AWS/Batch \
  --metric-name FailedJobCount \
  --dimensions Name=JobQueue,Value=${PROJECT}-queue \
  --statistic Sum --period 3600 --evaluation-periods 1 \
  --threshold 0 --comparison-operator GreaterThanThreshold \
  --alarm-actions ${SNS_ARN}
```

### 12.2 Alarma de calidad — MAPE de produccion

El pipeline ya loguea `business_oof_mape` como tag de MLflow. Esto NO está
disponible directamente en CW Metrics, así que extendemos el código del
trainer para emitir una metrica custom al final del run. Patch a aplicar
en `main.py` (o nuevo modulo `src/tracking/cloudwatch.py`):

```python
import boto3, os, json
def _emit_mape_metric(aggregate_path):
    """Emite business_oof_mape de cada variedad como custom metric en CW."""
    if not os.environ.get("EMIT_CW_METRICS"):
        return
    cw = boto3.client("cloudwatch")
    data = json.loads(open(aggregate_path).read())
    metric_data = []
    for variety, info in data.get("per_variety", {}).items():
        ch = info.get("champion") or {}
        mape = ch.get("champion_mape_oof_business")
        if mape is not None:
            metric_data.append({
                "MetricName":"BusinessMAPE",
                "Dimensions":[{"Name":"Variety","Value":variety}],
                "Value": float(mape),
                "Unit":"Percent",
            })
    if metric_data:
        cw.put_metric_data(Namespace="MLTraining", MetricData=metric_data)
```

Llamarlo despues de `_write_aggregate_summary(...)` en `main.py:152`. La
job definition ya pasa la env var apropiada — si querés activarlo, agregá
`EMIT_CW_METRICS=1` a las env vars del job-def.

Alarma:
```bash
aws cloudwatch put-metric-alarm \
  --alarm-name ${PROJECT}-mape-pop-too-high \
  --alarm-description "POP business MAPE > 25%" \
  --namespace MLTraining --metric-name BusinessMAPE \
  --dimensions Name=Variety,Value=POP \
  --statistic Average --period 86400 --evaluation-periods 1 \
  --threshold 25 --comparison-operator GreaterThanThreshold \
  --alarm-actions ${SNS_ARN}
```

### 12.3 Alarma: MLflow service down

```bash
TG_NAME=$(aws elbv2 describe-target-groups --target-group-arns ${TG_ARN} \
  --query 'TargetGroups[0].TargetGroupName' --output text)
LB_NAME=$(aws elbv2 describe-load-balancers --load-balancer-arns ${ALB_ARN} \
  --query 'LoadBalancers[0].LoadBalancerName' --output text)

aws cloudwatch put-metric-alarm \
  --alarm-name ${PROJECT}-mlflow-unhealthy \
  --namespace AWS/ApplicationELB \
  --metric-name UnHealthyHostCount \
  --dimensions Name=TargetGroup,Value=${TG_NAME} Name=LoadBalancer,Value=${LB_NAME} \
  --statistic Maximum --period 60 --evaluation-periods 5 \
  --threshold 0 --comparison-operator GreaterThanThreshold \
  --alarm-actions ${SNS_ARN}
```

---

## 13. GitHub Actions — CI/CD a ECR

`.github/workflows/build-and-push.yml`:

```yaml
name: build-and-push
on:
  push:
    branches: [main]
    paths:
      - "src/**"
      - "main.py"
      - "requirements.txt"
      - "Dockerfile"
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
          role-to-assume: arn:aws:iam::TU_ACCOUNT:role/ml-training-gh-actions
          aws-region: us-east-1
      - uses: aws-actions/amazon-ecr-login@v2
        id: ecr
      - name: Build & push
        env:
          ECR: ${{ steps.ecr.outputs.registry }}/ml-training
        run: |
          GIT_SHA=${GITHUB_SHA::7}
          docker buildx build --platform linux/amd64 \
            --build-arg GIT_SHA=${GIT_SHA} \
            --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
            --build-arg VERSION=${GITHUB_REF_NAME} \
            -t $ECR:${GIT_SHA} -t $ECR:latest --push .
      - name: Update Batch job-definition
        run: |
          DEF=$(aws batch describe-job-definitions \
            --job-definition-name ml-training --status ACTIVE \
            --query 'jobDefinitions[0]' --output json)
          NEW=$(echo "$DEF" | jq --arg img "${{ steps.ecr.outputs.registry }}/ml-training:latest" \
            '.containerProperties.image = $img |
             {jobDefinitionName, type, platformCapabilities, containerProperties,
              retryStrategy, timeout}')
          echo "$NEW" > /tmp/job-def.json
          aws batch register-job-definition --cli-input-json file:///tmp/job-def.json
```

OIDC role para GitHub (ejecutar UNA vez):

```bash
cat > /tmp/gh-trust.json <<JSON
{"Version":"2012-10-17","Statement":[{
  "Effect":"Allow",
  "Principal":{"Federated":"arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"},
  "Action":"sts:AssumeRoleWithWebIdentity",
  "Condition":{"StringEquals":{
    "token.actions.githubusercontent.com:aud":"sts.amazonaws.com"},
    "StringLike":{"token.actions.githubusercontent.com:sub":"repo:TU_ORG/ml_training:ref:refs/heads/main"}}}]}
JSON

aws iam create-role --role-name ${PROJECT}-gh-actions \
  --assume-role-policy-document file:///tmp/gh-trust.json

aws iam put-role-policy --role-name ${PROJECT}-gh-actions \
  --policy-name ecr-batch --policy-document '{
    "Version":"2012-10-17","Statement":[
      {"Effect":"Allow","Action":["ecr:GetAuthorizationToken"],"Resource":"*"},
      {"Effect":"Allow","Action":["ecr:BatchCheckLayerAvailability","ecr:CompleteLayerUpload",
        "ecr:InitiateLayerUpload","ecr:PutImage","ecr:UploadLayerPart"],
        "Resource":"arn:aws:ecr:'${AWS_REGION}':'${AWS_ACCOUNT_ID}':repository/'${ECR_REPO}'"},
      {"Effect":"Allow","Action":["batch:DescribeJobDefinitions","batch:RegisterJobDefinition"],
        "Resource":"*"},
      {"Effect":"Allow","Action":"iam:PassRole",
        "Resource":["arn:aws:iam::'${AWS_ACCOUNT_ID}':role/'${PROJECT}'-batch-exec",
                    "arn:aws:iam::'${AWS_ACCOUNT_ID}':role/'${PROJECT}'-batch-job"]}]}'
```

---

## 14. Smoke test end-to-end

```bash
source .aws_ids.env

# 1) Verifica que MLflow responde
curl -sf http://${ALB_DNS}/health && echo "MLflow OK"

# 2) Sube un Excel de prueba (debe disparar Lambda dispatcher por la
#    regla S3 ObjectCreated)
aws s3 cp data/training/DB-HISTORICA.xlsx \
  s3://${S3_DATA_BUCKET}/incoming/$(date +%Y-%m)/POP.xlsx

# 3) Verifica que Lambda fue invocado y submitio job
sleep 10
aws logs tail /aws/lambda/${PROJECT}-dispatcher --since 1m

# 4) Sigue logs del job en Batch
aws logs tail /aws/batch/${PROJECT} --follow

# 5) Cuando termine, deberias recibir email del SNS y ver run en MLflow:
echo "MLflow UI: http://${ALB_DNS}"
```

---

## 15. Operacion / runbook

### Re-entrenar manualmente una variedad

```bash
aws batch submit-job \
  --job-name "manual-$(date +%s)" \
  --job-queue ml-training-queue \
  --job-definition ml-training \
  --container-overrides '{"command":["--varieties","JUPITER","--tuning","prod"]}'
```

### Ver runs de MLflow

```bash
echo "http://${ALB_DNS}"   # abrir en navegador
```

### Cancelar un job en cola

```bash
aws batch terminate-job --job-id <id> --reason "manual cancel"
```

### Rollback de imagen

```bash
# Listar tags disponibles
aws ecr describe-images --repository-name ${ECR_REPO} \
  --query 'reverse(sort_by(imageDetails,& imagePushedAt))[*].[imageTags[0],imagePushedAt]' --output table

# Apuntar la job-def a un tag anterior
DEF=$(aws batch describe-job-definitions --job-definition-name ${PROJECT} \
  --status ACTIVE --query 'jobDefinitions[0]' --output json)
echo "$DEF" | jq --arg img "${ECR_URI}:abc1234" \
  '.containerProperties.image = $img |
   {jobDefinitionName,type,platformCapabilities,containerProperties,retryStrategy,timeout}' \
  > /tmp/rollback.json
aws batch register-job-definition --cli-input-json file:///tmp/rollback.json
```

### Bajar todo cuando no se usa (ahorrar costos)

```bash
# MLflow Fargate -> 0
aws ecs update-service --cluster ${PROJECT}-cluster \
  --service ${PROJECT}-mlflow --desired-count 0

# RDS -> stop (max 7 dias, despues vuelve a arrancar solo)
aws rds stop-db-instance --db-instance-identifier ${PROJECT}-mlflow

# Para volver:
aws rds start-db-instance --db-instance-identifier ${PROJECT}-mlflow
aws ecs update-service --cluster ${PROJECT}-cluster \
  --service ${PROJECT}-mlflow --desired-count 1
```

---

## 16. Adaptacion del codigo actual

Los unicos cambios obligatorios al repo son:

### 16.1 Nuevas env vars que el código ya respeta

`src/config.py` ya lee `MLFLOW_TRACKING_URI`, `S3_ARTIFACTS_BUCKET`,
`S3_ARTIFACTS_PREFIX`, `S3_REPORTS_PREFIX`. La job-def ya las pasa. Sin
cambios.

### 16.2 Data en S3 (acumulado -> split por variedad dentro del container)

**Flujo de datos:**
- Negocio sube SOLO `BD_HISTORICO_ACUMULADO.xlsx` al bucket data-raw.
- El container descarga ese acumulado y corre `scripts.prepare_data.split_workbook`
  para generar `data/training/DB-HISTORICA.xlsx` (una hoja por variedad).
- `main.py` continúa con su lógica habitual.

Esta logica YA esta aplicada en `main.py` (función `_hydrate_data_from_s3`).
Se activa cuando las env vars `S3_DATA_BUCKET` y `S3_DATA_KEY` estan presentes
(las inyecta el Lambda dispatcher en cada SubmitJob — ver §9). En local sin
esas env vars el flujo es no-op y asume que ya corriste `task data:split`.

Resumen del bloque clave (ya en `main.py`):

```python
from src.config import ACCUMULATED_FILE, MIN_ROWS_PER_VARIETY, TRAINING_FILE

def _hydrate_data_from_s3(logger) -> bool:
    bucket = os.environ.get("S3_DATA_BUCKET")
    key = os.environ.get("S3_DATA_KEY")
    if not (bucket and key):
        return True  # local: asume que prepare_data corrio offline
    import boto3
    from scripts.prepare_data import split_workbook
    ACCUMULATED_FILE.parent.mkdir(parents=True, exist_ok=True)
    boto3.client("s3").download_file(bucket, key, str(ACCUMULATED_FILE))
    split_workbook(
        input_path=ACCUMULATED_FILE,
        output_path=TRAINING_FILE,
        min_rows=MIN_ROWS_PER_VARIETY,
    )
    return True
```

El Lambda dispatcher (§9) pasa esas env vars como `containerOverrides.environment`
en cada `batch:SubmitJob`.

### 16.3 (Opcional) Emitir custom metric a CloudWatch

Ver §12.2.

---

## 17. Limpieza (al desarmar todo)

```bash
# Orden inverso al de creacion
aws ecs update-service --cluster ${PROJECT}-cluster --service ${PROJECT}-mlflow --desired-count 0
aws ecs delete-service --cluster ${PROJECT}-cluster --service ${PROJECT}-mlflow --force
aws ecs delete-cluster --cluster ${PROJECT}-cluster
aws elbv2 delete-load-balancer --load-balancer-arn ${ALB_ARN}
aws elbv2 delete-target-group --target-group-arn ${TG_ARN}
aws batch update-job-queue --job-queue ${PROJECT}-queue --state DISABLED
aws batch delete-job-queue --job-queue ${PROJECT}-queue
aws batch update-compute-environment --compute-environment ${PROJECT}-ce --state DISABLED
aws batch delete-compute-environment --compute-environment ${PROJECT}-ce
aws rds delete-db-instance --db-instance-identifier ${PROJECT}-mlflow --skip-final-snapshot
aws lambda delete-function --function-name ${PROJECT}-dispatcher
aws lambda delete-function --function-name ${PROJECT}-notifier
aws sns delete-topic --topic-arn ${SNS_ARN}
aws ecr delete-repository --repository-name ${ECR_REPO} --force
aws s3 rb s3://${S3_DATA_BUCKET} --force
aws s3 rb s3://${S3_MLFLOW_BUCKET} --force
aws ec2 delete-nat-gateway --nat-gateway-id ${NAT_ID}
# ... continuar con SGs, subnets, IGW, VPC
```

---

## 18. Checklist de implementacion

- [ ] §1 Variables base + AWS CLI configurada
- [ ] §2 VPC, subnets, NAT, security groups
- [ ] §3 Buckets S3
- [ ] §4 ECR + primera imagen pushed
- [ ] §5 Secret de DB
- [ ] §6 RDS Postgres up
- [ ] §7 MLflow Fargate behind ALB, /health responde
- [ ] §8 Batch compute env + queue + job-def
- [ ] §8.4 Smoke job manual en Batch corre OK
- [ ] §9 Lambda dispatcher
- [ ] §10 EventBridge schedule + S3 trigger
- [ ] §11 Lambda notifier + SNS suscrito + email confirmado
- [ ] §12 CloudWatch alarms
- [ ] §13 GitHub Actions OIDC + workflow
- [ ] §14 Smoke E2E: subir Excel a S3 dispara job, llega notificacion, run aparece en MLflow
- [ ] §16 Patches al codigo (`_hydrate_data_from_s3` + dispatcher con env vars)

---

## 19. Decisiones por revisar en el futuro

| Item | Hoy | Cuando reconsiderar |
|---|---|---|
| ALB publico para MLflow UI | SG restringido a tu IP | si el equipo crece > 3 personas, usar SSO via Cognito o cerrar el ALB y acceder via SSM port-forward |
| RDS db.t4g.micro | 20 GB, 1 AZ | si tracking pasa de ~10k runs/mes |
| Spot c6i.2xlarge | training 30-40 min/variety | si hay timeouts por interrupcion de Spot, subir `bidPercentage` o cambiar a on-demand |
| Single-region | us-east-1 | si hay requisito de DR |
| MLflow self-hosted | mantener compat con tu codigo | considerar SageMaker MLflow managed cuando GA en tu region |
| CloudWatch metrics manuales | EMIT_CW_METRICS opcional | adoptar `aws-embedded-metrics` library para emitir desde logs |
| Drift detection | no incluido | agregar Evidently/Whylogs scheduled job leyendo el aggregate.json |

#!/usr/bin/env bash
# Empaqueta el codigo (sin caches/datos/state) y lo sube a S3.
#
# CRITICO: excluimos `.env*` y `terraform.tfvars`. Si dejamos pasar el .env
# local del dev, al extraer el tar.gz en /opt/ml_training/ en la EC2,
# el .env de cloud-init (que tiene MLFLOW_TRACKING_URI=http://<private_ip>
# y PYTHON=/opt/ml_training/venv/bin/python) seria SOBRESCRITO por el
# .env del dev (que apunta a la EIP publica y a `python` del PATH). El
# training entonces correria con interprete sin deps y pasaria por internet
# en vez de la VPC. Mismo razonamiento para .env.infra y terraform.tfvars.
source "$(dirname "$0")/_common.sh"
require S3_BUCKET
TAR=/tmp/ml_training.tar.gz
log "empaquetando -> $TAR"
tar \
  --exclude='./mlruns' \
  --exclude='./artifacts' \
  --exclude='./logs' \
  --exclude='./reports' \
  --exclude='./infra/.terraform' \
  --exclude='./infra/modules/lambda_power/build' \
  --exclude='./infra/terraform.tfvars' \
  --exclude='./infra/*.tfstate' \
  --exclude='./infra/*.tfstate.*' \
  --exclude='./.git' \
  --exclude='./data' \
  --exclude='./__pycache__' \
  --exclude='./.env' \
  --exclude='./.env.infra' \
  -czf "$TAR" .
log "subiendo -> s3://$S3_BUCKET/code/ml_training.tar.gz"
aws s3 cp "$TAR" "s3://$S3_BUCKET/code/ml_training.tar.gz" \
  --region "$AWS_REGION"
log "ok"

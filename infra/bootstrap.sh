#!/usr/bin/env bash
# infra/bootstrap.sh — Bootstrap del backend Terraform.
# UNA VEZ por cuenta + region. Idempotente: re-ejecutar es seguro.
#
# Crea:
#   1) S3 bucket  ${PROJECT}-tfstate-${ACCOUNT_SUFFIX}  (state file Terraform)
#   2) DynamoDB   ${PROJECT}-tflock                     (state locking)
#   3) Service Linked Roles para Spot / ECS / Batch     (pre-creadas)
#
# El bucket S3 se crea via scripts/ensure-s3-bucket.sh (mismo helper que
# tasks/local.yml usa para data/artifacts). Asi el hardening
# (versioning + AES256 + public-access-block) vive en UN solo lugar.
#
# El sufijo se calcula via scripts/aws-suffix.sh (fuente unica). Los buckets
# de prod (data, artifacts, archive) usan el mismo sufijo de 7 digitos.

set -euo pipefail

PROJECT="${PROJECT:-ml-training}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
# Reusa ACCOUNT_SUFFIX de la sesion si ya esta exportado (Capitulo 3.5); sino
# lo calcula con el mismo script que tasks/local.yml -> garantiza coherencia
# entre buckets locales y de prod.
ACCOUNT_SUFFIX="${ACCOUNT_SUFFIX:-$(bash scripts/aws-suffix.sh)}"
TFSTATE_BUCKET="${PROJECT}-tfstate-${ACCOUNT_SUFFIX}"
LOCK_TABLE="${PROJECT}-tflock"

# 1) S3 bucket tfstate (delegado al helper compartido)
bash scripts/ensure-s3-bucket.sh "$TFSTATE_BUCKET" "$REGION"

# 2) DynamoDB lock table
if ! aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "$REGION" >/dev/null 2>&1; then
    echo "  $LOCK_TABLE  no existe -> creando..."
    aws dynamodb create-table --table-name "$LOCK_TABLE" \
        --attribute-definitions AttributeName=LockID,AttributeType=S \
        --key-schema AttributeName=LockID,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST --region "$REGION" >/dev/null
    aws dynamodb wait table-exists --table-name "$LOCK_TABLE" --region "$REGION"
    echo "  $LOCK_TABLE  CREADO"
else
    echo "  $LOCK_TABLE  EXISTE (reuso)"
fi

# 3) Service Linked Roles (errores "ya existe" se ignoran)
aws iam create-service-linked-role --aws-service-name spot.amazonaws.com   2>/dev/null || true
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com    2>/dev/null || true
aws iam create-service-linked-role --aws-service-name batch.amazonaws.com  2>/dev/null || true

echo "==> BOOTSTRAP COMPLETADO"
echo "    bucket=$TFSTATE_BUCKET  lock=$LOCK_TABLE  region=$REGION"

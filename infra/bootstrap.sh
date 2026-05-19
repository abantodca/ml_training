#!/usr/bin/env bash
# infra/bootstrap.sh — Bootstrap del backend Terraform.
# UNA VEZ por cuenta + region. Idempotente.

set -euo pipefail

PROJECT="${PROJECT:-ml-training}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
# Mismas convenciones que GUIA_MLOPS_AWS_V2.md §3.5 (ACCOUNT_ID / ACCOUNT_SUFFIX) —
# si el usuario ya las exporto en su sesion, las reusamos; sino las calculamos.
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

#!/usr/bin/env bash
# Crea bucket S3 si no existe + hardening minimo (versioning, AES256, no public).
# Idempotente: si ya existe, solo imprime "EXISTE (reuso)" y sale 0.
#
# Uso: ensure-s3-bucket.sh <name> <region>
# Consumido por tasks/local.yml `_ensure-bucket`.
set -euo pipefail

name="${1:?falta <name>}"
region="${2:?falta <region>}"

if aws s3api head-bucket --bucket "$name" 2>/dev/null; then
  echo "  $name  EXISTE (reuso)"
  exit 0
fi

echo "  $name  no existe -> creando..."

# us-east-1 NO acepta --create-bucket-configuration (es la default; AWS lo rechaza)
if [ "$region" = "us-east-1" ]; then
  aws s3api create-bucket --bucket "$name" --region "$region"
else
  aws s3api create-bucket --bucket "$name" --region "$region" \
    --create-bucket-configuration "LocationConstraint=$region"
fi

# Hardening minimo (mismas defaults que el modulo storage de prod)
aws s3api put-bucket-versioning --bucket "$name" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption --bucket "$name" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-public-access-block --bucket "$name" \
  --public-access-block-configuration \
  'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'

echo "  $name  CREADO (versioning + AES256 + no public)"

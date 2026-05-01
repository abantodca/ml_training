#!/usr/bin/env bash
# Descarga el acumulado desde S3 (uso en EC2).
source "$(dirname "$0")/_common.sh"
require S3_BUCKET
mkdir -p data
log "fetch s3://$S3_BUCKET/raw/BD_HISTORICO_ACUMULADO.xlsx"
aws s3 cp "s3://$S3_BUCKET/raw/BD_HISTORICO_ACUMULADO.xlsx" \
  data/ --region "$AWS_REGION"

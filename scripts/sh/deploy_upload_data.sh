#!/usr/bin/env bash
# Sube data/BD_HISTORICO_ACUMULADO.xlsx al bucket S3.
source "$(dirname "$0")/_common.sh"
require S3_BUCKET
local_path="data/BD_HISTORICO_ACUMULADO.xlsx"
[ -f "$local_path" ] || die "$local_path no existe"
log "uploading -> s3://$S3_BUCKET/raw/"
aws s3 cp "$local_path" "s3://$S3_BUCKET/raw/" --region "$AWS_REGION"
log "ok"

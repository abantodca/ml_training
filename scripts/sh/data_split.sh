#!/usr/bin/env bash
# Split del Excel acumulado por VARIEDAD.
source "$(dirname "$0")/_common.sh"
log "split por variedad..."
"$PYTHON" -m scripts.prepare_data \
  --input  data/BD_HISTORICO_ACUMULADO.xlsx \
  --output data/training/DB-HISTORICA.xlsx \
  --min-rows 100

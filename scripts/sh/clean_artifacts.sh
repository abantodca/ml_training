#!/usr/bin/env bash
# Limpia artifacts/ conservando los ultimos N runs por (variety, model).
# Args: KEEP (default 10) [--dry-run]
#
# MLflow mantiene el historial completo (mlruns/ o backend remoto), asi que
# esta limpieza NO pierde informacion: solo libera disco local.
source "$(dirname "$0")/_common.sh"

KEEP=${1:-10}
shift || true

log "limpiando artifacts/ | KEEP=$KEEP por (variety, model)"
"$PYTHON" -m scripts.clean_artifacts --keep "$KEEP" "$@"

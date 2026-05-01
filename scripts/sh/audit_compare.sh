#!/usr/bin/env bash
# Wrapper de scripts/audit_compare.py.
# Args opcionales se pasan tal cual al python:
#   task audit:compare                      -> todos los runs
#   task audit:compare -- --variety POP     -> filtra por variedad
#   task audit:compare -- --last 5          -> ultimos 5
#   task audit:compare -- --variety POP --model xgb --last 10
source "$(dirname "$0")/_common.sh"
"$PYTHON" scripts/audit_compare.py "$@"

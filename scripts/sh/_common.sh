#!/usr/bin/env bash
# Helpers compartidos por los scripts del Taskfile.
# Source-ar al inicio: `source "$(dirname "$0")/_common.sh"`

set -euo pipefail

# Default sano: si .env no override-a, `python` del PATH (con venv activo
# es lo deseable) corre el codigo. Override con PYTHON=... en .env.
: "${PYTHON:=python}"

# Logger compacto
log() { printf '\033[1;34m[task]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[task ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# require VAR1 VAR2 ... -> aborta si alguna esta vacia (sin uso real ahora
# que no hay infra; se mantiene por si algun script futuro la necesita).
require() {
  local missing=()
  for v in "$@"; do
    if [ -z "${!v:-}" ]; then missing+=("$v"); fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    die "Variables faltantes en .env: ${missing[*]}"
  fi
}

#!/usr/bin/env bash
# Helpers compartidos por los scripts del Taskfile.
# Source-ar al inicio: `source "$(dirname "$0")/_common.sh"`

set -euo pipefail

# Defaults sanos
: "${PYTHON:=python}"
: "${AWS_REGION:=us-east-1}"

# Logger compacto
log() { printf '\033[1;34m[task]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[task ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# require VAR1 VAR2 ... -> aborta si alguna esta vacia
require() {
  local missing=()
  for v in "$@"; do
    if [ -z "${!v:-}" ]; then missing+=("$v"); fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    die "Variables faltantes en .env: ${missing[*]}"
  fi
}

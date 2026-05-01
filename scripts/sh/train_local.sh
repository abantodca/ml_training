#!/usr/bin/env bash
# Training local multi-variedad + multi-modelo.
# Args: TUNING MODEL VARIETIES [PARALLEL=1] [EXTRA="--no-register"]
#
# TUNING   = presupuesto Optuna (smoke|dev|prod). NO es entorno.
# PARALLEL = numero de variedades a entrenar en PARALELO. Cada una corre en
#            su propio proceso (memoria liberada al terminar) y escribe
#            logs/variety_<NAME>.log. Recomendado:
#              - t3.large (2 cores)  -> PARALLEL=1 o 2
#              - c5.xlarge (4 cores) -> PARALLEL=2 o 3
#              - c5.2xlarge (8 c.)   -> PARALLEL=4
source "$(dirname "$0")/_common.sh"
TUNING=${1:-dev}
# MODEL=auto (default) -> entrena XGB y LGB cada uno con su Optuna independiente
# y elige campeon por variedad. Pasa "xgb" o "lgb" para forzar UN solo backend.
MODEL=${2:-auto}
VARIETIES=${3:-POP}
PARALLEL=${4:-1}
EXTRA=${5:-}

# Auto-split: si data/training/DB-HISTORICA.xlsx no existe, lo regeneramos
# desde data/BD_HISTORICO_ACUMULADO.xlsx. Si ya existe, se asume
# vigente y el training arranca directo (no re-genera, ahorra tiempo).
TRAINING_FILE="data/training/DB-HISTORICA.xlsx"
ACCUMULATED_FILE="data/BD_HISTORICO_ACUMULADO.xlsx"
if [ ! -f "$TRAINING_FILE" ]; then
  if [ ! -f "$ACCUMULATED_FILE" ]; then
    die "Faltan ambos: $TRAINING_FILE y $ACCUMULATED_FILE. Sube uno y reintenta."
  fi
  log "$TRAINING_FILE no existe; generando split desde $ACCUMULATED_FILE..."
  "$PYTHON" -m scripts.prepare_data \
    --input  "$ACCUMULATED_FILE" \
    --output "$TRAINING_FILE" \
    --min-rows 100
fi

"$PYTHON" main.py \
  --tuning "$TUNING" \
  --model "$MODEL" \
  --varieties "$VARIETIES" \
  --parallel-varieties "$PARALLEL" \
  $EXTRA

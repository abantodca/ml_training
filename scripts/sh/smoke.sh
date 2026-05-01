#!/usr/bin/env bash
# Pipeline completo local en modo smoke (~2 min con MODEL=auto, ~1 min con MODEL=xgb).
# Args: [MODEL=auto] [VARIETIES=POP]
# MODEL=auto -> entrena XGB y LGB y elige campeon. "xgb" o "lgb" fuerza uno.
source "$(dirname "$0")/_common.sh"
MODEL=${1:-auto}
VARIETIES=${2:-POP}
"$PYTHON" main.py --tuning smoke --model "$MODEL" --varieties "$VARIETIES"

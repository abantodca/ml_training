#!/usr/bin/env bash
# Lanza training en la EC2 via SSH. Args: TUNING MODEL VARIETIES PARALLEL
#
# TUNING acepta:    smoke | dev | prod  (presupuesto Optuna; NO es entorno)
# VARIETIES acepta: VENTURA | VENTURA,BIANCA,ATLAS | all
# MODEL acepta:     auto (default) | xgb | lgb | xgb,lgb | all
#                     auto = entrena XGB y LGB independientes y elige campeon
#                     por variedad (composite_score). "xgb" o "lgb" fuerza uno.
#
# Antes de entrenar refresca el codigo desde S3: el cloud-init solo
# descarga el tar.gz UNA vez al boot, asi que sin este sync correrias
# codigo viejo despues de un `task deploy:upload-code`.
source "$(dirname "$0")/_common.sh"
require SSH_KEY TRAINING_HOST S3_BUCKET
TUNING=${1:-prod}
MODEL=${2:-auto}
VARIETIES=${3:-POP}
PARALLEL=${4:-1}

log "ssh -> $TRAINING_HOST | tuning=$TUNING model=$MODEL varieties=$VARIETIES parallel=$PARALLEL"
# Quoting con comillas simples literales sobre el remote para que comas
# y espacios accidentales no rompan el parser de bash en la EC2.
#
# IMPORTANTE: invocamos `task train:_exec`, NO `task train:local`.
# `train:local` fuerza MLFLOW_TRACKING_URI="" (aislamiento del dev local
# para no contaminar el server remoto). Si lo usaramos aqui en la EC2,
# el training escribiria a file:// local en lugar del MLflow remoto.
# `train:_exec` ejecuta el mismo script sin ese override y respeta el
# URI del .env (apuntado al MLflow EC2 por cloud-init).
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "ubuntu@$TRAINING_HOST" \
  "set -euo pipefail && \
   cd /opt/ml_training && \
   echo '>> sync code from s3://$S3_BUCKET/code/ml_training.tar.gz' && \
   aws s3 cp 's3://$S3_BUCKET/code/ml_training.tar.gz' /tmp/code.tgz --region '$AWS_REGION' && \
   tar -xzf /tmp/code.tgz -C /opt/ml_training --no-same-owner && \
   /opt/ml_training/venv/bin/pip install -q -r /opt/ml_training/requirements.txt && \
   set -a && source .env && set +a && \
   task data:prepare && \
   task train:_exec TUNING='$TUNING' MODEL='$MODEL' VARIETIES='$VARIETIES' PARALLEL='$PARALLEL'"

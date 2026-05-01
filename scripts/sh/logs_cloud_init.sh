#!/usr/bin/env bash
# Log de boot (cloud-init) de una EC2. Arg: mlflow|training
source "$(dirname "$0")/_common.sh"
TARGET=${1:?'arg requerido: mlflow | training'}
require SSH_KEY
case "$TARGET" in
  mlflow)   require MLFLOW_HOST;   HOST=$MLFLOW_HOST ;;
  training) require TRAINING_HOST; HOST=$TRAINING_HOST ;;
  *) die "target invalido: $TARGET (usa mlflow | training)" ;;
esac
ssh -i "$SSH_KEY" "ubuntu@$HOST" 'sudo cat /var/log/cloud-init-output.log'

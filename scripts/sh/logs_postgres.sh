#!/usr/bin/env bash
# Tail al log de Postgres (self-hosted en la EC2 del MLflow).
source "$(dirname "$0")/_common.sh"
require SSH_KEY MLFLOW_HOST
ssh -i "$SSH_KEY" "ubuntu@$MLFLOW_HOST" \
  'sudo tail -f /var/log/postgresql/postgresql-*-main.log'

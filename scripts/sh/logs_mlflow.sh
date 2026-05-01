#!/usr/bin/env bash
# Tail al systemd journal del MLflow server.
source "$(dirname "$0")/_common.sh"
require SSH_KEY MLFLOW_HOST
ssh -i "$SSH_KEY" "ubuntu@$MLFLOW_HOST" 'sudo journalctl -u mlflow -f'

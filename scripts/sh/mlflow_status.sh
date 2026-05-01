#!/usr/bin/env bash
# Health check del MLflow server.
source "$(dirname "$0")/_common.sh"
require MLFLOW_HOST
url="http://$MLFLOW_HOST:5000/health"
if curl -fsS "$url" >/dev/null; then
  log "OK $url"
else
  die "MLflow NO responde en $url"
fi

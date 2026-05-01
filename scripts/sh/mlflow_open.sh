#!/usr/bin/env bash
# Abre la UI del MLflow remoto en el browser.
source "$(dirname "$0")/_common.sh"
require MLFLOW_HOST
url="http://$MLFLOW_HOST:5000"
log "abriendo $url"
"$PYTHON" -c "import webbrowser; webbrowser.open('$url')"

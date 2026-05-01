#!/usr/bin/env bash
# Espera a que las EC2 esten listas para recibir trabajo:
#   1) Lambda power:status reporta ambas instancias en 'running'
#   2) MLflow responde /health
#   3) SSH al training node responde
#
# Reemplaza el viejo `sleep 30` que era frecuentemente insuficiente para
# el primer boot (cloud-init tarda 60-120s) y ocioso en boots subsiguientes.
source "$(dirname "$0")/_common.sh"
require LAMBDA_POWER_NAME MLFLOW_HOST TRAINING_HOST SSH_KEY

MAX_WAIT_SECONDS=${WAIT_READY_MAX:-300}
SLEEP=${WAIT_READY_INTERVAL:-5}

log "esperando readiness (max ${MAX_WAIT_SECONDS}s) ..."

t0=$(date +%s)
elapsed() { echo $(( $(date +%s) - t0 )); }
abort_if_timeout() {
  if [ "$(elapsed)" -ge "$MAX_WAIT_SECONDS" ]; then
    die "timeout tras ${MAX_WAIT_SECONDS}s en: $1"
  fi
}

# 1) Power status -> both running
log "[1/3] esperando estado=running en EC2..."
while true; do
  OUT=$(mktemp)
  aws lambda invoke \
    --function-name "$LAMBDA_POWER_NAME" \
    --region "$AWS_REGION" \
    --cli-binary-format raw-in-base64-out \
    --payload '{"action":"status"}' \
    "$OUT" >/dev/null 2>&1 || true
  STOPPED=$("$PYTHON" -c "import json; d=json.load(open('$OUT')); print(len(d.get('stopped',[])))" 2>/dev/null || echo "?")
  RUNNING=$("$PYTHON" -c "import json; d=json.load(open('$OUT')); print(len(d.get('running',[])))" 2>/dev/null || echo "0")
  rm -f "$OUT"
  if [ "$STOPPED" = "0" ] && [ "$RUNNING" -ge "2" ]; then
    log "  ok | running=$RUNNING stopped=$STOPPED (dt=$(elapsed)s)"
    break
  fi
  printf '\r  running=%s stopped=%s dt=%ss   ' "$RUNNING" "$STOPPED" "$(elapsed)" >&2
  abort_if_timeout "power:status (running<2)"
  sleep "$SLEEP"
done

# 2) MLflow /health (cloud-init tarda en levantar postgres + mlflow)
log "[2/3] esperando MLflow /health en http://$MLFLOW_HOST:5000 ..."
while true; do
  if curl -fsS --max-time 3 "http://$MLFLOW_HOST:5000/health" >/dev/null 2>&1; then
    log "  ok (dt=$(elapsed)s)"
    break
  fi
  printf '\r  esperando mlflow... dt=%ss   ' "$(elapsed)" >&2
  abort_if_timeout "mlflow /health"
  sleep "$SLEEP"
done

# 3) SSH al training node
log "[3/3] esperando SSH a training node $TRAINING_HOST ..."
while true; do
  if ssh -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        -o ConnectTimeout=4 "ubuntu@$TRAINING_HOST" 'true' >/dev/null 2>&1; then
    log "  ok (dt=$(elapsed)s)"
    break
  fi
  printf '\r  esperando ssh... dt=%ss   ' "$(elapsed)" >&2
  abort_if_timeout "ssh training"
  sleep "$SLEEP"
done

log "EC2 listas en $(elapsed)s"

#!/usr/bin/env bash
# Invoca la Lambda power-manager. Arg: stop | start | status
source "$(dirname "$0")/_common.sh"
require LAMBDA_POWER_NAME
ACTION=${1:-status}
case "$ACTION" in
  stop|start|status) ;;
  *) die "action invalida: $ACTION (usa stop | start | status)" ;;
esac

OUT=$(mktemp)
log "invocando lambda $LAMBDA_POWER_NAME action=$ACTION"
aws lambda invoke \
  --function-name "$LAMBDA_POWER_NAME" \
  --region "$AWS_REGION" \
  --cli-binary-format raw-in-base64-out \
  --payload "$(printf '{"action":"%s"}' "$ACTION")" \
  "$OUT" >/dev/null
"$PYTHON" -c "import json,sys; d=json.load(open('$OUT')); print(json.dumps(d, indent=2))"
rm -f "$OUT"

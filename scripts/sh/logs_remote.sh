#!/usr/bin/env bash
# Tail al log del pipeline en la EC2 de training.
source "$(dirname "$0")/_common.sh"
require SSH_KEY TRAINING_HOST
ssh -i "$SSH_KEY" "ubuntu@$TRAINING_HOST" \
  'tail -f /opt/ml_training/logs/pipeline_run.log'

#!/usr/bin/env bash
# Tail al log local del pipeline.
source "$(dirname "$0")/_common.sh"
[ -f logs/pipeline_run.log ] || die "logs/pipeline_run.log no existe; corre algun training primero"
tail -f logs/pipeline_run.log

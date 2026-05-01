#!/usr/bin/env bash
# Borra outputs locales (artifacts/, logs/, reports/) y caches Python
# (__pycache__/). NO toca data/ ni mlruns/ ni el codigo.
source "$(dirname "$0")/_common.sh"
log "borrando artifacts/, logs/, reports/, __pycache__/"
rm -rf artifacts/* logs/* reports/* 2>/dev/null || true
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
log "ok"

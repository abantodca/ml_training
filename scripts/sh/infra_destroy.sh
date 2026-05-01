#!/usr/bin/env bash
# Destruye toda la infra. Bloqueante: pide confirmacion explicita.
source "$(dirname "$0")/_common.sh"
read -r -p "Vas a destruir TODA la infra (S3 + EC2 + Lambda + EIPs). Escribe 'destroy' para continuar: " ans
[ "$ans" = "destroy" ] || die "abortado"
( cd infra && terraform destroy -auto-approve )

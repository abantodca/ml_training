#!/bin/bash
# Cloud-init: bootstrap del training node.
#
# Instala Python + go-task + AWS CLI + clona/descarga el codigo + instala deps.
# Deja un .env preconfigurado para que `task train:local` funcione sin tocar nada.
#
# Variables interpoladas por Terraform:
#   ${s3_bucket}            -> bucket S3 con raw + code
#   ${region}               -> region AWS
#   ${mlflow_private_ip}    -> IP privada del MLflow server (talk dentro de la VPC)
#   ${code_archive_s3_uri}  -> donde subio `task deploy:upload-code` el tar.gz
#   ${git_repo_url}         -> URL HTTPS del repo (vacio = no clonar)
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-pip python3-venv git curl unzip ca-certificates \
  build-essential libgomp1 awscli rsync wget

# go-task (Taskfile runner) - bin global
curl -sL https://taskfile.dev/install.sh | sh -s -- -b /usr/local/bin

# usuario y dirs
APP_DIR=/opt/ml_training
mkdir -p "$APP_DIR"
chown -R ubuntu:ubuntu "$APP_DIR"

# 1) Si hay git_repo_url => clone; si no, baja tar.gz desde S3
if [ -n "${git_repo_url}" ]; then
  sudo -u ubuntu git clone "${git_repo_url}" "$APP_DIR"
else
  TMP_TAR=/tmp/ml_training.tar.gz
  aws s3 cp "${code_archive_s3_uri}" "$TMP_TAR" --region ${region} || true
  if [ -f "$TMP_TAR" ]; then
    sudo -u ubuntu tar -xzf "$TMP_TAR" -C "$APP_DIR"
  else
    echo "ADVERTENCIA: no hay codigo todavia. Sube con 'task deploy:upload-code' y reinstala luego."
  fi
fi

# venv
sudo -u ubuntu python3 -m venv "$APP_DIR/venv"
if [ -f "$APP_DIR/requirements.txt" ]; then
  sudo -u ubuntu "$APP_DIR/venv/bin/pip" install --upgrade pip
  sudo -u ubuntu "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
fi

# .env apuntando al MLflow server por IP privada (dentro de la VPC).
# PYTHON apunta al venv para que `task` (corriendo via SSH no-interactivo)
# use el interprete con todas las deps instaladas.
cat >"$APP_DIR/.env" <<ENV
AWS_REGION=${region}
S3_BUCKET=${s3_bucket}
MLFLOW_TRACKING_URI=http://${mlflow_private_ip}:5000
MLFLOW_EXPERIMENT_PREFIX=productivity_
MODEL_REGISTRY_PREFIX=productivity_
PYTHON=$APP_DIR/venv/bin/python
ENV
chown ubuntu:ubuntu "$APP_DIR/.env"

# Hacer que el venv se active al hacer SSH
echo "source $APP_DIR/venv/bin/activate" >> /home/ubuntu/.bashrc
echo "cd $APP_DIR" >> /home/ubuntu/.bashrc
echo "set -a; source $APP_DIR/.env 2>/dev/null; set +a" >> /home/ubuntu/.bashrc

# ----- CloudWatch Agent -----
# Envia los logs del pipeline (logs/*.log) y cloud-init a CloudWatch Logs.
# IAM role ya trae CloudWatchAgentServerPolicy (modules/iam/main.tf).
mkdir -p "$APP_DIR/logs"
chown -R ubuntu:ubuntu "$APP_DIR/logs"

CW_DEB=/tmp/amazon-cloudwatch-agent.deb
wget -q -O "$CW_DEB" \
  "https://s3.${region}.amazonaws.com/amazoncloudwatch-agent-${region}/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb" \
  || wget -q -O "$CW_DEB" \
  "https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb"
dpkg -i -E "$CW_DEB" || apt-get install -fy

mkdir -p /opt/aws/amazon-cloudwatch-agent/etc
cat >/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<CWCFG
{
  "agent": { "run_as_user": "root" },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "$APP_DIR/logs/*.log",
            "log_group_name": "/ml-training/training/pipeline",
            "log_stream_name": "{instance_id}/{file_basename}",
            "retention_in_days": 30
          },
          {
            "file_path": "/var/log/cloud-init-output.log",
            "log_group_name": "/ml-training/training/cloud-init",
            "log_stream_name": "{instance_id}",
            "retention_in_days": 30
          }
        ]
      }
    }
  }
}
CWCFG

/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json || true

echo "training node listo en $APP_DIR"

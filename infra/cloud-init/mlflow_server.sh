#!/bin/bash
# Cloud-init: bootstrap del MLflow tracking server con Postgres self-hosted.
#
# Stack:
#   - Postgres 14 (apt) en localhost:5432, db `mlflow`, user `mlflow`
#   - Python 3 + venv + mlflow + boto3 + psycopg2-binary
#   - systemd unit "mlflow.service" -> mlflow server en :5000
#       backend-store-uri  : postgresql://mlflow:<pwd>@127.0.0.1:5432/mlflow
#       artifact-root      : s3://<bucket>/mlflow-artifacts
#       --serve-artifacts  : el server hace de proxy hacia S3
#
# SEGURIDAD: el password de Postgres se genera LOCALMENTE con `openssl rand`
# y vive solo en /etc/mlflow/db.env (perms 0600). NO viaja por user_data
# ni por el state de Terraform. Esto previene que cualquiera con
# `ec2:DescribeInstanceAttribute` pueda leer el password.
#
# Variables interpoladas por Terraform (templatefile):
#   $${s3_bucket}    -> bucket destino de artifacts
#   $${region}       -> region AWS
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-pip python3-venv \
  postgresql postgresql-contrib libpq-dev \
  awscli curl ca-certificates openssl wget

# ----- Postgres -----
systemctl enable postgresql
systemctl start postgresql

# Password generado UNA vez en el primer boot. Persiste a stop/start.
mkdir -p /etc/mlflow
if [ ! -f /etc/mlflow/db.env ]; then
  pwd=$(openssl rand -hex 24)
  echo "DB_PASSWORD=$pwd" > /etc/mlflow/db.env
  chmod 600 /etc/mlflow/db.env
fi
DB_PASSWORD=$(grep -E '^DB_PASSWORD=' /etc/mlflow/db.env | cut -d= -f2-)

# Idempotente: crea o actualiza el rol y la db.
EXISTS_ROLE=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='mlflow'" || true)
if [ "$EXISTS_ROLE" != "1" ]; then
  sudo -u postgres psql -c "CREATE USER mlflow WITH PASSWORD '$DB_PASSWORD';"
else
  sudo -u postgres psql -c "ALTER USER mlflow WITH PASSWORD '$DB_PASSWORD';"
fi

EXISTS_DB=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='mlflow'" || true)
if [ "$EXISTS_DB" != "1" ]; then
  sudo -u postgres psql -c "CREATE DATABASE mlflow OWNER mlflow;"
fi

# ----- usuario y dirs MLflow -----
useradd -m -s /bin/bash mlflow || true
mkdir -p /opt/mlflow
chown -R mlflow:mlflow /opt/mlflow
# Permitir que mlflow user lea su EnvironmentFile via systemd (root lo lee)
chgrp mlflow /etc/mlflow/db.env
chmod 640 /etc/mlflow/db.env

# ----- venv y deps -----
sudo -u mlflow python3 -m venv /opt/mlflow/venv
sudo -u mlflow /opt/mlflow/venv/bin/pip install --upgrade pip
sudo -u mlflow /opt/mlflow/venv/bin/pip install \
  "mlflow>=2.10,<3" \
  "boto3>=1.28" \
  "psutil>=5.9" \
  "psycopg2-binary>=2.9"

# ----- systemd unit -----
# Heredoc single-quoted: bash NO expande variables; los $${VAR} (escapados
# desde Terraform a $${VAR}) los substituye systemd al iniciar el servicio
# usando EnvironmentFile.
cat >/etc/systemd/system/mlflow.service <<'UNIT'
[Unit]
Description=MLflow Tracking Server (Postgres backend, S3 artifacts)
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=mlflow
Group=mlflow
WorkingDirectory=/opt/mlflow
EnvironmentFile=/etc/mlflow/db.env
Environment=AWS_DEFAULT_REGION=__REGION__
ExecStart=/opt/mlflow/venv/bin/mlflow server \
  --host 0.0.0.0 \
  --port 5000 \
  --backend-store-uri postgresql://mlflow:$${DB_PASSWORD}@127.0.0.1:5432/mlflow \
  --default-artifact-root s3://__BUCKET__/mlflow-artifacts \
  --serve-artifacts
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# Substituye placeholders __REGION__ / __BUCKET__ con los valores que
# Terraform interpolo en este script.
sed -i "s|__REGION__|${region}|g; s|__BUCKET__|${s3_bucket}|g" /etc/systemd/system/mlflow.service

systemctl daemon-reload
systemctl enable mlflow
systemctl start mlflow

# ----- CloudWatch Agent -----
# Envia a CloudWatch Logs:
#   - journalctl -u mlflow         -> /ml-training/mlflow/server
#   - /var/log/postgresql/*.log    -> /ml-training/mlflow/postgres
#   - /var/log/cloud-init-output   -> /ml-training/mlflow/cloud-init
# El IAM role ya trae CloudWatchAgentServerPolicy (modules/iam/main.tf).
CW_DEB=/tmp/amazon-cloudwatch-agent.deb
wget -q -O "$CW_DEB" \
  "https://s3.${region}.amazonaws.com/amazoncloudwatch-agent-${region}/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb" \
  || wget -q -O "$CW_DEB" \
  "https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb"
dpkg -i -E "$CW_DEB" || apt-get install -fy

mkdir -p /opt/aws/amazon-cloudwatch-agent/etc
cat >/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<'CWCFG'
{
  "agent": { "run_as_user": "root" },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/postgresql/postgresql-*.log",
            "log_group_name": "/ml-training/mlflow/postgres",
            "log_stream_name": "{instance_id}",
            "retention_in_days": 30
          },
          {
            "file_path": "/var/log/cloud-init-output.log",
            "log_group_name": "/ml-training/mlflow/cloud-init",
            "log_stream_name": "{instance_id}",
            "retention_in_days": 30
          }
        ]
      }
    }
  }
}
CWCFG

# Ademas, capturar journalctl -u mlflow via systemd-journal -> file -> agent.
# Mas simple que el plugin "journald" del agent (que no esta en todas las versiones).
cat >/etc/systemd/system/mlflow-journal-tail.service <<'UNIT'
[Unit]
Description=Tail mlflow journal a /var/log/mlflow.log para CloudWatch
After=mlflow.service
Requires=mlflow.service

[Service]
Type=simple
ExecStart=/bin/bash -c '/bin/journalctl -u mlflow -f --no-tail >> /var/log/mlflow.log'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
touch /var/log/mlflow.log
chmod 644 /var/log/mlflow.log
systemctl daemon-reload
systemctl enable --now mlflow-journal-tail

# Anadir /var/log/mlflow.log al config del agent (post-creacion para mantener
# el heredoc de arriba simple y evitar errores de sintaxis JSON al editar).
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json")
cfg = json.loads(p.read_text())
cfg["logs"]["logs_collected"]["files"]["collect_list"].insert(0, {
    "file_path": "/var/log/mlflow.log",
    "log_group_name": "/ml-training/mlflow/server",
    "log_stream_name": "{instance_id}",
    "retention_in_days": 30,
})
p.write_text(json.dumps(cfg, indent=2))
PY

/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json || true

# ----- health check -----
for i in $(seq 1 60); do
  if curl -fsS http://localhost:5000/health >/dev/null 2>&1; then
    echo "mlflow up after $i intentos"
    exit 0
  fi
  sleep 2
done
echo "ADVERTENCIA: mlflow no respondio en 120s. Ver: journalctl -u mlflow -n 200"
exit 0

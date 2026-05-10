# Para builds 100% deterministicos en CI, fijar digest:
#   FROM ghcr.io/mlflow/mlflow:v3.12.0@sha256:<digest>
FROM ghcr.io/mlflow/mlflow:v3.12.0

# Versiones pinneadas (drift silencioso si se dejan flotando).
# psycopg2-binary 2.9.9: ultima 2.9.x estable contra Postgres 15/16.
# boto3 1.38.x: alinea con el rango del trainer en requirements.txt.
RUN pip install --no-cache-dir \
        psycopg2-binary==2.9.9 \
        boto3==1.38.0

LABEL org.opencontainers.image.title="mlflow-with-pg-s3" \
      org.opencontainers.image.description="MLflow 3.12.0 + psycopg2-binary + boto3 (Postgres backend + S3 artifacts)" \
      org.opencontainers.image.source="https://github.com/abantodca/ml_training" \
      org.opencontainers.image.base.name="ghcr.io/mlflow/mlflow:v3.12.0"

# syntax=docker/dockerfile:1.7
# Pin a minor + variant para reproducibilidad. Para builds 100% deterministicos
# en CI, sustituir por digest: `python:3.13.1-slim-bookworm@sha256:<digest>`.
ARG PYTHON_VERSION=3.13.1-slim-bookworm

# ============================================================
# Stage 1: builder — compila wheels con build deps
# ============================================================
FROM python:${PYTHON_VERSION} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./

# Cache mount: pip cache persiste entre builds (BuildKit)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /wheels -r requirements.txt

# ============================================================
# Stage 2: runtime — solo lo necesario para correr
# ============================================================
FROM python:${PYTHON_VERSION} AS runtime

# OCI labels — se llenan desde --build-arg en CI
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown
ARG VERSION=dev
LABEL org.opencontainers.image.title="ml-training" \
    org.opencontainers.image.description="Random Forest training pipeline" \
    org.opencontainers.image.source="https://github.com/abantodca/ml_training" \
    org.opencontainers.image.revision="${GIT_SHA}" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_HOME=/app

# Runtime deps + tini para reaping/SIGTERM correcto en Batch.
# `git` se instala porque mlflow.utils.git_utils + collect_run_metadata
# dependen de el para taggear el run con git_commit. Sin git en el
# container, todos los runs salen con git_commit=unknown -> auditoria
# de compliance rota (no podes vincular un modelo en Registry a un SHA).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 ca-certificates tini git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 mluser \
    && useradd  --system --uid 1001 --gid mluser --home ${APP_HOME} mluser

WORKDIR ${APP_HOME}

# Instala desde wheels pre-construidos (sin compiladores en runtime)
COPY --from=builder /wheels /wheels
COPY requirements.txt ./
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# Codigo (orden de mejor a peor cache)
COPY --chown=mluser:mluser src/    ./src/
COPY --chown=mluser:mluser scripts/ ./scripts/
COPY --chown=mluser:mluser main.py  ./

# Carpetas que init_dirs() asume (idempotente). Nota: NO creamos `mlruns/`
# porque el backend MLflow es siempre el server (Postgres + S3).
RUN mkdir -p data/training logs artifacts reports \
    && chown -R mluser:mluser ${APP_HOME}

USER mluser

# Explicito para auditores: tini reenvia SIGTERM al python child.
STOPSIGNAL SIGTERM

# tini propaga SIGTERM correctamente cuando Batch mata el job
ENTRYPOINT ["/usr/bin/tini", "--", "python", "main.py"]

# Default smoke (red de seguridad para `docker run` sin args).
# compose / Batch SIEMPRE lo sobreescriben con --varieties / --tuning reales.
CMD ["--varieties", "POP", "--tuning", "smoke"]
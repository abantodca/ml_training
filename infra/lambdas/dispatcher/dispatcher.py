"""Lambda dispatcher: entrypoint manual para AWS Batch SubmitJob.

Invocacion tipica:
    aws lambda invoke --function-name ml-training-dispatcher \
        --payload '{"detail":{"varieties":"POP,JUPITER","tuning":"prod"}}' \
        --cli-binary-format raw-in-base64-out /tmp/out.json

Tambien admite override del input data:
    --payload '{"detail":{"varieties":"POP","tuning":"prod","s3_data_bucket":"...","s3_data_key":"..."}}'
"""

import os
import re
import boto3

batch = boto3.client("batch")
JOB_QUEUE = os.environ["JOB_QUEUE"]  # spot queue (default)
JOB_QUEUE_OD = os.environ["JOB_QUEUE_OD"]  # on-demand queue (solo para prod_xl)
JOB_DEFINITION = os.environ["JOB_DEFINITION"]
DEFAULT_TUNING = os.environ.get("DEFAULT_TUNING", "prod")
DEFAULT_BUCKET = os.environ.get("DEFAULT_DATA_BUCKET", "")
DEFAULT_KEY = os.environ.get("DEFAULT_DATA_KEY", "")
VARIETIES_ALLOWED = set(os.environ.get("VARIETIES_ALLOWED", "").split(","))


def _pick_queue(tuning: str) -> str:
    # prod_xl (5-6h wallclock, P(spot interrupt) ~ 20-30%) -> on-demand.
    # smoke/dev/prod (1.5-2h) -> spot con retry=2.
    return JOB_QUEUE_OD if tuning == "prod_xl" else JOB_QUEUE


def _validate(varieties: str) -> list[str]:
    parsed = [v.strip().upper() for v in varieties.split(",") if v.strip()]
    if not parsed:
        raise ValueError("varieties vacio")
    invalid = [v for v in parsed if v not in VARIETIES_ALLOWED]
    if invalid:
        raise ValueError(
            f"variedades no permitidas: {invalid}. Allowed: {sorted(VARIETIES_ALLOWED)}"
        )
    return parsed


def lambda_handler(event, context):
    detail = (event or {}).get("detail", {})
    varieties_raw = detail.get("varieties")
    tuning = detail.get("tuning", DEFAULT_TUNING)
    s3_bucket = detail.get("s3_data_bucket", DEFAULT_BUCKET)
    s3_key = detail.get("s3_data_key", DEFAULT_KEY)

    if not varieties_raw:
        raise ValueError("falta 'varieties' en el payload (ej. 'POP' o 'POP,JUPITER')")
    if not (s3_bucket and s3_key):
        raise ValueError(
            "falta s3_data_bucket / s3_data_key (ni override ni defaults configurados)"
        )

    _validate(varieties_raw)
    queue = _pick_queue(tuning)
    varieties_arg = ",".join(_validate(varieties_raw))

    job_name = re.sub(r"[^a-zA-Z0-9_-]", "-", f"train-{varieties_arg}-{tuning}")[:128]
    resp = batch.submit_job(
        jobName=job_name,
        jobQueue=queue,
        jobDefinition=JOB_DEFINITION,
        containerOverrides={
            "command": ["--varieties", varieties_arg, "--tuning", tuning],
            "environment": [
                {"name": "S3_DATA_BUCKET", "value": s3_bucket},
                {"name": "S3_DATA_KEY", "value": s3_key},
            ],
        },
    )
    return {
        "jobId": resp["jobId"],
        "queue": queue,
        "varieties": varieties_arg,
        "tuning": tuning,
        "s3_data": f"s3://{s3_bucket}/{s3_key}",
    }

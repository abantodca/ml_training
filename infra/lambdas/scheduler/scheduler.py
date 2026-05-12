"""Lambda mlflow-scheduler: enciende o apaga MLflow + Reports + RDS Postgres.

Disparado por:
  - EventBridge cron L-V 13 UTC (= 08 PET) con {"action": "start"} -> wake up services
  - EventBridge cron L-V 17 UTC (= 12 PET) con {"action": "stop"}  -> shutdown
  - GitHub Actions train.yml (Sec 7.4) con {"action": "start"} / {"action": "stop"}

Anti-conflicto: el shutdown chequea Batch jobs RUNNING/RUNNABLE/STARTING antes de
apagar. Si hay alguno activo, NO apaga (deja el sistema up; el siguiente cron del
otro dia volvera a chequear). Esto cubre el caso "training cruza el limite de 12 PM"
sin race conditions.

Idempotente: si ya esta en el estado destino, no hace nada.
"""

import os

import boto3
from botocore.exceptions import ClientError

ecs = boto3.client("ecs")
rds = boto3.client("rds")
batch = boto3.client("batch")

ECS_CLUSTER = os.environ["ECS_CLUSTER"]
ECS_SERVICE_MLFLOW = os.environ["ECS_SERVICE_MLFLOW"]
ECS_SERVICE_REPORTS = os.environ["ECS_SERVICE_REPORTS"]
RDS_INSTANCE_ID = os.environ["RDS_INSTANCE_ID"]
BATCH_QUEUES = [q for q in os.environ.get("BATCH_QUEUES", "").split(",") if q]

ECS_SERVICES = [ECS_SERVICE_MLFLOW, ECS_SERVICE_REPORTS]

# Estados de Batch que indican "job todavia consumiendo MLflow" — incluye RUNNABLE
# porque puede ya haber abierto conexion durante init aunque no este corriendo full.
ACTIVE_BATCH_STATUSES = ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING")


def _has_active_batch_jobs() -> tuple[bool, list[dict]]:
    """Returns (any_active, list_of_active_jobs_summary)."""
    active = []
    for queue in BATCH_QUEUES:
        for status in ACTIVE_BATCH_STATUSES:
            resp = batch.list_jobs(jobQueue=queue, jobStatus=status, maxResults=10)
            for j in resp.get("jobSummaryList", []):
                active.append({"jobId": j["jobId"], "queue": queue, "status": status})
    return bool(active), active


def _ecs_set_desired_count(service: str, target: int) -> dict:
    resp = ecs.describe_services(cluster=ECS_CLUSTER, services=[service])
    svc = resp["services"][0]
    if svc["desiredCount"] == target:
        return {"service": service, "result": "noop", "desiredCount": target}
    ecs.update_service(cluster=ECS_CLUSTER, service=service, desiredCount=target)
    return {
        "service": service,
        "result": "updated",
        "from": svc["desiredCount"],
        "to": target,
    }


def _rds_transition(action: str) -> dict:
    resp = rds.describe_db_instances(DBInstanceIdentifier=RDS_INSTANCE_ID)
    status = resp["DBInstances"][0]["DBInstanceStatus"]
    try:
        if action == "start" and status == "stopped":
            rds.start_db_instance(DBInstanceIdentifier=RDS_INSTANCE_ID)
            return {"rds": "starting", "from": status}
        if action == "stop" and status == "available":
            rds.stop_db_instance(DBInstanceIdentifier=RDS_INSTANCE_ID)
            return {"rds": "stopping", "from": status}
    except ClientError as exc:
        return {"rds": "error", "code": exc.response["Error"]["Code"], "status": status}
    return {"rds": "noop", "status": status}


def lambda_handler(event, context):
    action = (event or {}).get("action", "")
    if action not in ("start", "stop"):
        raise ValueError(f"action invalido: {action!r} (esperado: start|stop)")

    if action == "stop":
        # Anti-conflicto: si hay training corriendo, NO apagar — esperar al
        # proximo cron. Esto cubre "training pasa 12 PM" sin matar el job.
        any_active, jobs = _has_active_batch_jobs()
        if any_active:
            return {
                "action": "stop",
                "result": "deferred",
                "reason": "active_batch_jobs_present",
                "active_jobs": jobs,
            }

    # Orden de operaciones:
    # - start: ambos ECS + RDS en paralelo (MLflow Fargate retry-ea hasta que RDS este up).
    # - stop: ECS primero (drena ALB target group), RDS despues (sin queries colgando).
    target = 1 if action == "start" else 0
    ecs_results = [_ecs_set_desired_count(svc, target) for svc in ECS_SERVICES]
    rds_result = _rds_transition(action)

    return {"action": action, "result": "applied", "ecs": ecs_results, **rds_result}

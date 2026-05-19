"""Lambda scheduler: start/stop RDS + Fargate.

Acciones:
- start:    wake secuencial RDS -> MLflow -> Reports (patch 13.3).
- stop:     baja ECS services a 0 + para RDS. Antes chequea Batch jobs RUNNING.
- keepstop: cada 6h. Si RDS quedo RUNNING fuera de ventana, lo re-para.
            Usa WORKDAYS_CRON + WORK_START_UTC + WORK_END_UTC del env (patch 13.1).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

ecs = boto3.client("ecs")
rds = boto3.client("rds")
batch = boto3.client("batch")

ECS_CLUSTER = os.environ["ECS_CLUSTER"]
ECS_SVC_MLFLOW = os.environ["ECS_SVC_MLFLOW"]
ECS_SVC_REPORTS = os.environ["ECS_SVC_REPORTS"]
RDS_INSTANCE = os.environ["RDS_INSTANCE"]
JOB_QUEUE_SPOT = os.environ["JOB_QUEUE_SPOT"]
JOB_QUEUE_ONDEMAND = os.environ["JOB_QUEUE_ONDEMAND"]

# Patch 13.1: parametrizacion del keepstop. Defaults conservan el comportamiento
# original (L-V 08-12 PET = 13-17 UTC) si las env vars no llegan al Lambda.
_WEEKDAY_MAP = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def _parse_workdays(cron_token: str) -> set[int]:
    """Parsea 'MON,WED,FRI' o 'MON-FRI' a un set de tm_wday."""
    cron_token = cron_token.strip().upper()
    if "-" in cron_token:
        a, b = cron_token.split("-", 1)
        ia, ib = _WEEKDAY_MAP[a.strip()], _WEEKDAY_MAP[b.strip()]
        return set(range(ia, ib + 1))
    return {_WEEKDAY_MAP[tok.strip()] for tok in cron_token.split(",") if tok.strip()}


def _running_jobs() -> list[str]:
    """IDs de jobs en estado RUNNING/RUNNABLE/STARTING en cualquiera de las queues."""
    ids: list[str] = []
    for queue in (JOB_QUEUE_SPOT, JOB_QUEUE_ONDEMAND):
        for status in ("RUNNING", "RUNNABLE", "STARTING"):
            resp = batch.list_jobs(jobQueue=queue, jobStatus=status)
            ids.extend(j["jobId"] for j in resp.get("jobSummaryList", []))
    return ids


def _rds_state() -> str:
    return rds.describe_db_instances(DBInstanceIdentifier=RDS_INSTANCE)["DBInstances"][0]["DBInstanceStatus"]


def _wait_until(check: Callable[[int], bool], *, label: str, max_iters: int, sleep_s: int = 10) -> bool:
    """Polling generico. `check(i)` retorna True cuando el recurso esta listo.

    Devuelve True si la condicion se cumplio, False si se agoto el timeout.
    """
    for i in range(max_iters):
        if check(i):
            return True
        log.info("%s[%d] no listo, sleep %ds", label, i, sleep_s)
        time.sleep(sleep_s)
    return False


def _start():
    """Wake secuencial: RDS -> MLflow -> Reports (patch 13.3).

    En la version original las 3 acciones iban en paralelo. Esto generaba
    `connection refused` en MLflow mientras RDS arrancaba (~5 min cold start)
    y ocasionalmente el healthcheck del task tumbaba el container antes de
    que RDS estuviera listo.
    """
    log.info("=== START (secuencial: RDS -> MLflow -> Reports) ===")

    # Etapa 1: RDS. start_db_instance solo es valido desde 'stopped'; otros
    # estados (starting/available/...) significan que ya esta arrancando o lo esta.
    if _rds_state() == "stopped":
        rds.start_db_instance(DBInstanceIdentifier=RDS_INSTANCE)
        log.info("rds start_db_instance ack")

    def rds_available(_i: int) -> bool:
        state = _rds_state()
        log.info("rds=%s", state)
        return state == "available"

    if not _wait_until(rds_available, label="rds", max_iters=48):  # max ~8 min
        raise RuntimeError(f"RDS no available tras 8 min (estado={_rds_state()})")

    log.info("rds OK -> arrancando MLflow")

    # Etapa 2: MLflow Fargate
    ecs.update_service(cluster=ECS_CLUSTER, service=ECS_SVC_MLFLOW, desiredCount=1)
    log.info("ecs %s -> desiredCount=1", ECS_SVC_MLFLOW)

    def mlflow_running(_i: int) -> bool:
        svc = ecs.describe_services(cluster=ECS_CLUSTER, services=[ECS_SVC_MLFLOW])["services"][0]
        running = svc.get("runningCount", 0)
        log.info("mlflow: running=%d desired=%d", running, svc.get("desiredCount", 0))
        return running >= 1

    if not _wait_until(mlflow_running, label="mlflow", max_iters=30):  # max ~5 min
        log.warning("MLflow no esta running tras 5 min, arrancamos reports igual")

    # Etapa 3: Reports Fargate (no-bloqueante: no depende de RDS ni MLflow)
    ecs.update_service(cluster=ECS_CLUSTER, service=ECS_SVC_REPORTS, desiredCount=1)
    log.info("ecs %s -> desiredCount=1", ECS_SVC_REPORTS)
    log.info("=== START OK ===")


def _stop():
    log.info("=== STOP ===")
    running = _running_jobs()
    if running:
        log.warning(
            "Batch jobs activos (%d): %s. Postponiendo stop hasta proximo cron.",
            len(running),
            running[:5],
        )
        return

    for svc in (ECS_SVC_MLFLOW, ECS_SVC_REPORTS):
        ecs.update_service(cluster=ECS_CLUSTER, service=svc, desiredCount=0)
        log.info("ecs %s -> desiredCount=0", svc)

    state = _rds_state()
    if state == "available":
        rds.stop_db_instance(DBInstanceIdentifier=RDS_INSTANCE)
        log.info("rds stop_db_instance ack")
    else:
        log.info("rds en estado %s (skip stop)", state)


def _keepstop():
    """Defense: si RDS quedo RUNNING fuera de ventana, re-pararlo.

    Patch 13.1: parametriza workdays + horas desde el env (antes era
    `weekday < 5 and 13 <= utc_hour < 17` hardcoded).
    """
    log.info("=== KEEPSTOP ===")
    workdays = _parse_workdays(os.environ.get("WORKDAYS_CRON", "MON-FRI"))
    start_utc = int(os.environ.get("WORK_START_UTC", "13"))
    end_utc = int(os.environ.get("WORK_END_UTC", "17"))

    now = time.gmtime()
    in_window = (now.tm_wday in workdays) and (start_utc <= now.tm_hour < end_utc)
    if in_window:
        log.info(
            "dentro de ventana (UTC=%02d:00, weekday=%d, workdays=%s), skip",
            now.tm_hour, now.tm_wday, sorted(workdays),
        )
        return

    state = _rds_state()
    if state != "available":
        log.info("rds en estado %s (skip)", state)
        return

    if _running_jobs():
        log.warning("Batch jobs activos, skip keepstop")
        return

    rds.stop_db_instance(DBInstanceIdentifier=RDS_INSTANCE)
    log.info("rds re-stopped por keepstop")


_ACTIONS = {"start": _start, "stop": _stop, "keepstop": _keepstop}


def handler(event, _context):
    action = (event or {}).get("action", "stop")
    handler_fn = _ACTIONS.get(action)
    if handler_fn is None:
        raise ValueError(f"action desconocida: {action}. Validas: {sorted(_ACTIONS)}")
    handler_fn()
    return {"statusCode": 200, "body": action}

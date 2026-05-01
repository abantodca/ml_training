"""Lambda: power manager para las EC2 del proyecto.

Acciones soportadas (vienen en `event["action"]`):
    - "stop"   : detiene todas las EC2 con tag Project=<PROJECT_NAME>
                 (Postgres y MLflow paran porque corren via systemd dentro
                  de la EC2; al detener la instancia se detiene todo).
    - "start"  : arranca las que esten 'stopped'. Postgres y mlflow.service
                 vuelven solos por systemd (Requires=postgresql.service).
    - "status" : devuelve el estado actual (running/stopped/other) sin tocar
                 nada. Idempotente y barato.

Filtrado: SIEMPRE por tag `Project=$PROJECT_NAME`, nunca toca instancias de
otros proyectos. La env var PROJECT_NAME viene seteada por Terraform.

Output:
    {
        "project": "<name>",
        "action": "stop|start|status",
        "targets": ["i-..."],
        "state_before": {"running": [...], "stopped": [...], "other": [...]}
    }
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

import boto3

ec2 = boto3.client("ec2")


def _describe_by_project(project: str) -> Dict[str, List[str]]:
    """Devuelve {state: [instance_ids]} para todas las EC2 del proyecto."""
    resp = ec2.describe_instances(
        Filters=[{"Name": "tag:Project", "Values": [project]}]
    )
    by_state: Dict[str, List[str]] = {"running": [], "stopped": [], "other": []}
    for reservation in resp.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            state = instance["State"]["Name"]
            iid = instance["InstanceId"]
            (by_state[state] if state in by_state else by_state["other"]).append(iid)
    return by_state


def lambda_handler(event: Dict[str, Any] | None, context) -> Dict[str, Any]:
    action = (event or {}).get("action", "status")
    project = os.environ["PROJECT_NAME"]

    state = _describe_by_project(project)

    if action == "status":
        return {"project": project, "action": action, **state}

    if action == "stop":
        targets = state["running"]
        if targets:
            ec2.stop_instances(InstanceIds=targets)
    elif action == "start":
        targets = state["stopped"]
        if targets:
            ec2.start_instances(InstanceIds=targets)
    else:
        raise ValueError(
            f"action desconocida: {action!r}. Usa stop | start | status."
        )

    return {
        "project": project,
        "action": action,
        "targets": targets,
        "state_before": state,
    }

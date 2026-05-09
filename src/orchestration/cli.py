"""CLI parsing y resolucion de argumentos para el pipeline de training.

Vive aqui (separado de `main.py`) porque es la unica capa que:
  - Conoce el contrato CLI (argparse).
  - Traduce strings ("all") a listas tipadas.
  - Resuelve overrides de tuning profile (n_trials, folds, etc.).

`main.py` queda thin: parse + delega en `orchestration.runners`.

Nota: el pipeline SIEMPRE entrena todos los backends del registry
(`valid_backends()`) y delega la eleccion del campeon a
`champion.select_champion`. No hay flag de usuario para forzar un modelo:
ese es el contrato del proyecto (la maquina elige, no el operador).
"""
from __future__ import annotations

import argparse

from src.config import (
    DEFAULT_TUNING,
    DEFAULT_VARIETIES,
    MLFLOW_EXPERIMENT_PREFIX,
    TUNING_PROFILES,
)
from src.step_01_load.data_loader import list_varieties


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pipeline ML - Nested CV + Optuna multi-variedad + multi-modelo",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--tuning",
        choices=list(TUNING_PROFILES),
        default=DEFAULT_TUNING,
        help="Presupuesto de Optuna: smoke (~1 min) | dev (~10 min) | prod (~1.5 h).",
    )
    parser.add_argument(
        "--varieties",
        default=DEFAULT_VARIETIES,
        help='Lista CSV de variedades (= hojas) o "all" para todas.',
    )
    parser.add_argument("--n-trials", type=int, default=None)
    parser.add_argument("--final-trials", type=int, default=None)
    parser.add_argument("--outer-folds", type=int, default=None)
    parser.add_argument("--inner-folds", type=int, default=None)
    parser.add_argument("--skip-final-tuning", action="store_true")
    parser.add_argument(
        "--experiment-prefix",
        default=MLFLOW_EXPERIMENT_PREFIX,
        help=(
            "Prefijo de experimentos. Cada variedad va a su propio experimento "
            "(prefix + variety). Ej: 'productivity_' -> 'productivity_POP'."
        ),
    )
    parser.add_argument(
        "--no-register",
        dest="register_model",
        action="store_false",
        default=True,
        help="Desactiva el registro en MLflow Model Registry (default: registra)",
    )
    parser.add_argument(
        "--registry-stage",
        choices=["None", "Staging", "Production"],
        default="None",
        help=(
            "Stage para la nueva version registrada (default: None = registra "
            "sin promover). Pasa 'Staging' o 'Production' explicitamente cuando "
            "quieras promover. Asi evitamos que entrenamientos dev ensucien el "
            "Registry con versiones en Staging."
        ),
    )
    parser.add_argument(
        "--parallel-varieties",
        type=int,
        default=1,
        help=(
            "Variedades a entrenar en PARALELO (procesos independientes). "
            "Cada subproceso libera su memoria al terminar (cache cleanup "
            "natural). Recomendado: 2-4 segun cores disponibles."
        ),
    )
    parser.add_argument(
        "--inner-cv-n-jobs",
        type=int,
        default=-1,
        help=(
            "n_jobs para cross_val_score interno. -1 = todos los cores. "
            "Si --parallel-varieties > 1, se ajusta automaticamente para "
            "evitar oversubscription."
        ),
    )
    return parser.parse_args(argv)


def resolve_settings(args: argparse.Namespace) -> dict[str, int | bool | str]:
    """Aplica overrides CLI sobre el tuning profile base."""
    base = dict(TUNING_PROFILES[args.tuning])
    overrides = {
        "n_trials": args.n_trials,
        "final_trials": args.final_trials,
        "outer_folds": args.outer_folds,
        "inner_folds": args.inner_folds,
    }
    for k, v in overrides.items():
        if v is not None:
            base[k] = v
    base["skip_final_tuning"] = args.skip_final_tuning
    return base


def resolve_varieties(arg: str) -> list[str]:
    if arg.strip().lower() == "all":
        return list_varieties()
    return [v.strip() for v in arg.split(",") if v.strip()]

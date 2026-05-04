"""CLI parsing y resolucion de argumentos para el pipeline de training.

Vive aqui (separado de `main.py`) porque es la unica capa que:
  - Conoce el contrato CLI (argparse).
  - Traduce strings ("all", "xgb,lgb") a listas tipadas.
  - Resuelve overrides de tuning profile (n_trials, folds, etc.).

`main.py` queda thin: parse + delega en `orchestration.runners`.
"""
from __future__ import annotations

import argparse

from src.config import (
    DEFAULT_TUNING,
    DEFAULT_VARIETIES,
    MLFLOW_EXPERIMENT_PREFIX,
    MODEL_TYPE_DEFAULT,
    STACKING_DEFAULT,
    TUNING_PROFILES,
)
from src.step_01_load.data_loader import list_varieties
from src.step_04_train.registry import valid_backends
from src.step_04_train.search_spaces import META_SEARCH_SPACE_REGISTRY

VALID_MODELS = valid_backends()
VALID_STACKING = ["none"] + sorted(META_SEARCH_SPACE_REGISTRY)


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
        "--model",
        default=MODEL_TYPE_DEFAULT,
        help=(
            'Modelos a entrenar (CSV). Ej: "xgb", "lgb", "xgb,lgb", "all", "auto". '
            '"auto" (default) = entrena todos los backends, cada uno con su Optuna '
            "independiente, y elige campeon por variedad (composite_score). "
            'Pasa "xgb" o "lgb" si quieres forzar uno solo.'
        ),
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
    parser.add_argument(
        "--stacking",
        choices=VALID_STACKING,
        default=STACKING_DEFAULT,
        help=(
            "Meta-learner sobre el campeon (XGB/LGB). 'none' (default) = "
            "el campeon predice directo. 'gam' = envuelve en StackedRegressor "
            "con un pyGAM como meta. El backend NO necesita cambios: el "
            "wrapper expone predict() como cualquier pipeline. Costo extra: "
            "~1x el tiempo del refit final por el cross_val_predict del meta."
        ),
    )
    parser.add_argument(
        "--meta-trials",
        type=int,
        default=None,
        help=(
            "Trials de Optuna para tunear el GAM meta (n_splines, lam) "
            "sobre las OOF preds del Stacked. None (default) = usa el "
            "presupuesto del perfil de tuning. 0 = sin tuning, usa los "
            "defaults de config. Solo aplica si --stacking=gam."
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
        "meta_trials": args.meta_trials,
    }
    for k, v in overrides.items():
        if v is not None:
            base[k] = v
    base["skip_final_tuning"] = args.skip_final_tuning
    # Stacking flag viaja por settings para que single_run lo lea sin
    # tener que conocer el namespace argparse completo.
    base["stacking"] = args.stacking
    return base


def resolve_varieties(arg: str) -> list[str]:
    if arg.strip().lower() == "all":
        return list_varieties()
    return [v.strip() for v in arg.split(",") if v.strip()]


def resolve_models(arg: str) -> list[str]:
    token = arg.strip().lower()
    if token in ("all", "auto"):
        return list(VALID_MODELS)
    models = [m.strip().lower() for m in arg.split(",") if m.strip()]
    invalid = [m for m in models if m not in VALID_MODELS]
    if invalid:
        raise ValueError(
            f"--model invalido: {invalid}. Validos: {VALID_MODELS} + 'all' / 'auto'."
        )
    seen: set[str] = set()
    return [m for m in models if not (m in seen or seen.add(m))]

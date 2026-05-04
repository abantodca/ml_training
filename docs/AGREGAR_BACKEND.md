# Cómo agregar un nuevo backend de modelo

El proyecto separa el modelo del resto del pipeline a través de
`src/step_04_train/registry.py`. Para enchufar un nuevo backend (CatBoost,
TabNet, GAM puro, sktime, skforecast, etc.) **solo hay que tocar 4 lugares**.
Ninguno de los pasos requiere editar el orchestrator (`variety_runner.py`,
`single_run.py`) ni el evaluador (`champion.py`, `feature_importance.py`).

> Antes de empezar: el modelo nuevo debe cumplir el contrato sklearn:
> métodos `.fit(X, y[, sample_weight])` y `.predict(X)`. Si no, envolverlo
> en un wrapper que herede de `BaseEstimator + RegressorMixin` (ver
> `src/step_04_train/oof_ensemble.py:OOFEnsembleRegressor` como ejemplo).

---

## Paso 1 — Crear `src/step_04_train/model_<nombre>.py`

Función factory que devuelve el regresor con defaults sanos. Patrón mínimo:

```python
# src/step_04_train/model_catboost.py
from catboost import CatBoostRegressor
from sklearn.compose import TransformedTargetRegressor

from src.config import RANDOM_STATE
from src.step_04_train.target_transform import wrap_with_log_target


def get_catboost_model(**overrides) -> TransformedTargetRegressor:
    """CatBoostRegressor envuelto en log-target transformer."""
    params = dict(
        random_state=RANDOM_STATE,
        verbose=False,
        thread_count=1,
    )
    params.update(overrides)
    return wrap_with_log_target(CatBoostRegressor(**params))
```

**Por qué `wrap_with_log_target`**: el target `KG/JR_H` tiene cola larga.
Todos los backends actuales lo usan para predecir en log-space y
estabilizar el ajuste de outliers. Usalo si tu modelo no tiene su propia
transformación de target.

---

## Paso 2 — Definir el search space en `search_spaces.py`

Función `suggest_<nombre>_params(trial)` que devuelve el dict de
hiperparámetros con prefijo `regressor__regressor__`:

```python
# src/step_04_train/search_spaces.py (agregar al final)
def suggest_catboost_params(trial: optuna.Trial) -> Dict[str, object]:
    """Search space anti-overfitting para CatBoost."""
    return {
        "regressor__regressor__iterations": trial.suggest_int(
            "regressor__regressor__iterations", 200, 1500
        ),
        "regressor__regressor__depth": trial.suggest_int(
            "regressor__regressor__depth", 3, 6
        ),
        "regressor__regressor__learning_rate": trial.suggest_float(
            "regressor__regressor__learning_rate", 1e-2, 0.3, log=True
        ),
        "regressor__regressor__l2_leaf_reg": trial.suggest_float(
            "regressor__regressor__l2_leaf_reg", 1.0, 20.0, log=True
        ),
        # ... etc
    }
```

**Convención de prefijos**: `preprocessor__<step>__<param>` para
hiperparámetros del preprocesador, `regressor__regressor__<param>` para
los del modelo (doble prefijo porque el modelo está envuelto en
`TransformedTargetRegressor`).

---

## Paso 3 — Registrar en `registry.py`

Una sola línea en `BACKEND_REGISTRY`:

```python
# src/step_04_train/registry.py
from src.step_04_train.model_catboost import get_catboost_model
from src.step_04_train.search_spaces import suggest_catboost_params

BACKEND_REGISTRY: Dict[str, ModelBackend] = {
    "xgb": ModelBackend("xgb", get_xgb_model, suggest_xgb_params),
    "lgb": ModelBackend("lgb", get_lgb_model, suggest_lgb_params),
    "catboost": ModelBackend("catboost", get_catboost_model, suggest_catboost_params),
}
```

A partir de aquí ya podés correr:

```
task train VARIETIES=POP MODEL=catboost
task train VARIETIES=POP MODEL=catboost,xgb,lgb
```

`cli.py` y `tuning.py` leen del registry — ambos detectan el nuevo backend
automáticamente sin más cambios.

---

## Paso 4 — Agregar la dependencia a `requirements.txt`

```
catboost==1.2.7
```

Y `pip install -r requirements.txt`.

---

## ¿Qué pasa si el modelo no soporta `sample_weight`?

`tuning.py` y `oof_ensemble.py` lo pasan via
`fit_with_optional_sample_weight()` (ver
`src/utils/sklearn_helpers.py`). Si el modelo no acepta ese kwarg, el
helper detecta el TypeError y reintenta sin pesos. **No hay que tocar
nada** — el contrato es "pasalo si podés, ignoralo si no".

## ¿Qué pasa con SHAP feature importance?

`compute_feature_importance` usa `shap.TreeExplainer`, que soporta XGB,
LGB, CatBoost, sklearn-trees nativamente. Para modelos NO basados en
árboles (TabNet, redes, GAM puro), habría que extender
`feature_importance.py` para usar `shap.PermutationExplainer` como
fallback. Ese cambio NO es requisito para enchufar el backend; el
training y los runs MLflow funcionan con o sin SHAP.

## ¿Y el champion / decisión inter-backend?

`champion.py` solo mira `metrics`, `business_metrics_oof`,
`elapsed_seconds` y opcionalmente `stacking_diagnostics`. **No hace
asunciones del tipo de modelo.** Cualquier backend que llene esas
métricas compite igualitariamente. Los umbrales viven en `config.py`
(`GAP_TIE_TOLERANCE`, `FULL_MAPE_TIE_TOLERANCE`,
`META_PREFERENCE_DELTA_PCT`).

---

## Checklist completo de PR

- [ ] `src/step_04_train/model_<nombre>.py` con `get_<nombre>_model()`
- [ ] `src/step_04_train/search_spaces.py` con `suggest_<nombre>_params()`
- [ ] `src/step_04_train/registry.py` con la entrada nueva en
      `BACKEND_REGISTRY`
- [ ] `requirements.txt` con la dependencia pinneada
- [ ] Smoke test: `task train VARIETIES=POP MODEL=<nombre> TUNING=smoke`
- [ ] Verificar que el run aparece en MLflow UI con su `model_type` tag

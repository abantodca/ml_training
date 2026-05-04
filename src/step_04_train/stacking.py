"""StackedRegressor: campeon (XGB/LGB) + meta GAM en cascada.

Diseno
------
Internamente:
    fit(X, y):
        1. KFold(n_oof_folds) sobre X.
        2. Para cada fold: clone(base) + fit(train) + predict(test).
           Resultado: oof_pred (1 prediccion honesta por fila).
        3. Construye `meta_features` = [oof_pred] + cols transformadas
           del subset (continuas imputadas, categoricas label-encoded,
           flags __ISNAN auto-generadas).
        4. self.meta_model_.fit(meta_features, y, weights=sample_weight).
        5. self.base_pipeline_ = clone(base) + fit(X, y, sample_weight).
           (Refit total para inferencia: el base de produccion ve TODO X.)
        6. Auto-fallback: compara MAE del base puro vs MAE del meta sobre
           oof_pred. Si meta es peor por mas de un margen pequeno,
           desactiva el meta y `predict()` cae al base. Garantia de que
           stacking nunca empeora.

    predict(X):
        pred_base = self.base_pipeline_.predict(X)
        if not self._meta_active_:
            return pred_base                        # auto-fallback activado
        meta_features = construye_subset_transformado(X, pred_base)
        return self.meta_model_.predict(meta_features)

Por que cross_val_predict y NO reusar OOF del nested CV:
    El nested CV del base (en tuning.perform_nested_cv) TUNEA
    hiperparametros por fold; cada fold puede tener best_params distintos.
    Eso da OOF heterogeneas que un meta-learner aprende con sesgo. Aqui
    el base ya tiene best_params FIJOS (los que pico el nested CV final),
    asi que un KFold simple genera OOF homogeneas y honestas para el meta.

Backend / serving
-----------------
StackedRegressor cumple el contrato sklearn (fit/predict + atributos `_`),
asi que `mlflow.sklearn.log_model` lo serializa como un opaque pipeline.
El backend hace `mlflow.pyfunc.load_model().predict(X_raw)` igual que con
OOFEnsembleRegressor: la cascada base->meta es interna al wrapper.

Categoricas y flags
-------------------
El X que llega aqui es el RAW (antes del preprocessing del base, que
incluye CustomKNNImputer + one-hot + lags). `x_subset_cols` viene tal
cual del Excel. Para alimentar al GAM:

    - Continuas con NaN ratio bajo:    fillna(median del fit).
    - Continuas con NaN ratio alto:    fillna(median) + flag <col>__ISNAN
                                       (0/1, factor en el GAM).
    - Categoricas (string/object):     label-encode con map memorizado
                                       en fit. Niveles nuevos en predict
                                       caen al sentinel -1, que se marca
                                       como factor adicional.

Esto evita que el GAM trate strings como nan, y que el spline se pegue
al pico de la mediana cuando hay >10% de NaN.
"""
from __future__ import annotations

import logging
import time
import warnings
from typing import Dict, List, Optional, Tuple

import numpy as np
import optuna
import pandas as pd
from optuna.exceptions import ExperimentalWarning
from sklearn.base import BaseEstimator, RegressorMixin, clone
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import KFold
from sklearn.pipeline import Pipeline

from src.config import (
    STACKING_AUTO_FALLBACK,
    STACKING_FALLBACK_RELATIVE_MARGIN,
    STACKING_META_INNER_FOLDS,
    STACKING_NAN_FLAG_THRESHOLD,
)
from src.step_04_train.model_gam import get_gam_meta_model
from src.utils.sklearn_helpers import (
    fit_with_optional_sample_weight,
    index_or_none,
)

warnings.filterwarnings("ignore", category=ExperimentalWarning)

logger = logging.getLogger(__name__)

# Nota sobre niveles categoricos no vistos en inferencia:
#   pyGAM `f()` valida que todos los valores caigan en [0, n_levels-1]
#   del rango aprendido en fit (un sentinel -1 crashea predict).
#   Por eso al label-encodear mapeamos cualquier nivel no visto al
#   MODAL level del fit (mas frecuente). Es la fallback mas conservadora:
#   trata al nivel desconocido como "el tipico". El group-rare upstream
#   (config.RARE_GROUP_COLS) ya colapsa los niveles raros a 'OTROS' en
#   train, asi que rara vez se ejecuta este branch.


class StackedRegressor(BaseEstimator, RegressorMixin):
    """Wrapper de stacking: base_pipeline -> meta GAM con cat support.

    Parametros
    ----------
    base_pipeline : Pipeline (preprocessor + regressor) o `OOFEnsembleRegressor`.
                    NO debe estar fiteado: `clone()` requiere unfitted.
    x_subset_cols : columnas RAW (pre-preprocessing) que el meta recibe
                    ademas de pred_base. Mantener corto (3-7).
                    Categoricas (string) y continuas se mezclan: el
                    detector interno decide el termino del GAM.
    n_oof_folds   : K del KFold interno para construir oof_pred. Default 5.
    gam_n_splines : nodos por spline. Default config.
    gam_lam       : penalizacion smoothness. Default config.
    monotonic_pred_base : constraint creciente sobre s(pred_base). Default True.
    nan_flag_threshold : NaN ratio a partir del cual se auto-genera la
                         flag <col>__ISNAN. Default config.
    auto_fallback : si True (default), verifica que meta mejore al base
                    sobre oof; si no, desactiva meta y predict cae al base.
    tune_meta_trials : numero de trials de Optuna para tunear (gam_n_splines,
                       gam_lam) sobre las OOF preds del propio stacked. 0
                       (default) = sin tuning, usa los defaults pasados en
                       gam_n_splines / gam_lam. Si el tuning falla, cae a
                       defaults y registra warning.
    tune_meta_inner_folds : KFold interno del Optuna del meta. Default 3.
                            Separado del KFold de oof_pred (n_oof_folds) para
                            evitar overfit del meta a sus propias OOF.
    random_state  : seed del KFold.

    Atributos post-fit (con guion bajo)
    -----------------------------------
    base_pipeline_      : pipeline base refiteado sobre TODO X.
    meta_model_         : LinearGAM fiteado.
    oof_pred_           : ndarray (n,) preds OOF que vio el meta.
    x_subset_medians_   : Series con mediana por col continua del fit.
    cat_encoders_       : dict {col -> {level_str: int}} para label-encode.
    cat_modal_level_    : dict {col -> int} con el codigo del nivel mas
                          frecuente en train. Se usa como fallback para
                          niveles no vistos en inferencia (pyGAM no acepta
                          valores fuera del rango del fit).
    nan_flag_cols_      : list[str] con cols del subset que tienen flag.
    meta_feature_names_ : nombres de las cols vistas por el GAM (para HTML).
    n_meta_features_    : len(meta_feature_names_).
    cat_indices_        : indices (en meta_features) de cols categoricas.
    _meta_active_       : True si el meta se usa en predict; False = fallback.
    _decision_log_      : dict con MAE_base / MAE_meta / decision (para audit).
    tuned_meta_params_  : dict con (gam_n_splines, gam_lam) finales. Si no
                          hubo tuning, son los defaults pasados al __init__.
    tuning_log_         : dict con n_trials, best_score, elapsed; vacio si
                          tune_meta_trials=0.
    """

    def __init__(
        self,
        base_pipeline: Pipeline,
        x_subset_cols: List[str],
        n_oof_folds: int = 5,
        gam_n_splines: int = 15,
        gam_lam: float = 0.6,
        monotonic_pred_base: bool = True,
        nan_flag_threshold: float = STACKING_NAN_FLAG_THRESHOLD,
        auto_fallback: bool = STACKING_AUTO_FALLBACK,
        tune_meta_trials: int = 0,
        tune_meta_inner_folds: int = STACKING_META_INNER_FOLDS,
        random_state: int = 42,
    ):
        self.base_pipeline = base_pipeline
        self.x_subset_cols = x_subset_cols
        self.n_oof_folds = n_oof_folds
        self.gam_n_splines = gam_n_splines
        self.gam_lam = gam_lam
        self.monotonic_pred_base = monotonic_pred_base
        self.nan_flag_threshold = nan_flag_threshold
        self.auto_fallback = auto_fallback
        self.tune_meta_trials = tune_meta_trials
        self.tune_meta_inner_folds = tune_meta_inner_folds
        self.random_state = random_state

    # ------------------------------------------------------------------
    # Helpers: deteccion y transformacion del subset
    # ------------------------------------------------------------------
    def _validate_x_subset(self, X: pd.DataFrame) -> None:
        missing = [c for c in self.x_subset_cols if c not in X.columns]
        if missing:
            raise ValueError(
                f"StackedRegressor: x_subset_cols faltan en X: {missing}. "
                f"Disponibles: {list(X.columns)}"
            )

    @staticmethod
    def _is_categorical(series: pd.Series) -> bool:
        """Categorica si dtype object/string/category. Numeric -> False."""
        return series.dtype == object or pd.api.types.is_string_dtype(series) \
            or isinstance(series.dtype, pd.CategoricalDtype)

    def _build_meta_inputs(
        self,
        X: pd.DataFrame,
        pred_base: np.ndarray,
        *,
        learn: bool,
    ) -> Tuple[np.ndarray, List[str], List[int]]:
        """Construye la matriz que ve el GAM.

        Si `learn=True` (durante fit): aprende y memoriza medians,
        cat_encoders, nan_flag_cols. Si `learn=False` (durante predict):
        usa los memorizados.

        Returns
        -------
        meta_features : ndarray (n, K) listo para gam.fit/predict.
        names         : nombres de las K columnas (col 0 = "pred_base").
        cat_indices   : indices de columnas categoricas en meta_features.
        """
        cols_data: List[np.ndarray] = [pred_base.reshape(-1, 1)]
        names: List[str] = ["pred_base"]
        cat_indices: List[int] = []

        if learn:
            self.x_subset_medians_ = pd.Series(dtype=float)
            self.cat_encoders_ = {}
            self.cat_modal_level_ = {}
            self.nan_flag_cols_ = []

        col_offset = 1  # col 0 ya es pred_base
        for col in self.x_subset_cols:
            series = X[col]
            is_cat = self._is_categorical(series)

            if is_cat:
                # ---- Categorica: label-encode (memorizado) ----
                if learn:
                    str_series = series.astype(str)
                    levels = sorted(str_series.dropna().unique().tolist())
                    self.cat_encoders_[col] = {lvl: i for i, lvl in enumerate(levels)}
                    # Modal level del fit: lo usamos como fallback para
                    # cualquier nivel desconocido en inferencia (pyGAM no
                    # acepta valores fuera del rango del fit).
                    modal_str = str_series.mode().iloc[0]
                    self.cat_modal_level_[col] = self.cat_encoders_[col][modal_str]
                encoder = self.cat_encoders_[col]
                fallback_code = self.cat_modal_level_[col]
                encoded = series.astype(str).map(encoder).fillna(fallback_code)
                arr = encoded.to_numpy(dtype=float).reshape(-1, 1)
                cols_data.append(arr)
                names.append(col)
                cat_indices.append(col_offset)
                col_offset += 1
            else:
                # ---- Continua: imputar mediana + maybe flag ----
                series_f = pd.to_numeric(series, errors="coerce")
                if learn:
                    median_val = float(series_f.median())
                    if not np.isfinite(median_val):
                        # Columna 100% NaN en train: degenerar a 0 (safe).
                        median_val = 0.0
                    self.x_subset_medians_[col] = median_val
                    nan_ratio = float(series_f.isna().mean())
                    if nan_ratio > self.nan_flag_threshold:
                        self.nan_flag_cols_.append(col)

                imputed = series_f.fillna(self.x_subset_medians_[col])
                cols_data.append(imputed.to_numpy(dtype=float).reshape(-1, 1))
                names.append(col)
                col_offset += 1

                # Flag binaria para cols con high-NaN. Es cat en el GAM
                # (factor 0/1) en vez de spline -- evita que el spline
                # alise el paso observed/imputed. La decision learn vs
                # predict ya se hizo arriba (al rellenar nan_flag_cols_),
                # asi que aqui solo consultamos.
                if col in self.nan_flag_cols_:
                    flag_arr = series_f.isna().astype(int).to_numpy().reshape(-1, 1)
                    cols_data.append(flag_arr.astype(float))
                    names.append(f"{col}__ISNAN")
                    cat_indices.append(col_offset)
                    col_offset += 1

        meta_features = np.hstack(cols_data)
        return meta_features, names, cat_indices

    @staticmethod
    def _evaluate_fallback(
        y_true: np.ndarray,
        oof_pred: np.ndarray,
        meta_pred: np.ndarray,
        margin: float = STACKING_FALLBACK_RELATIVE_MARGIN,
    ) -> Tuple[bool, float, float]:
        """Decide si el meta mejora suficientemente al base.

        Devuelve (meta_active, mae_base, mae_meta).
        """
        mae_base = float(mean_absolute_error(y_true, oof_pred))
        mae_meta = float(mean_absolute_error(y_true, meta_pred))
        # meta_active si: meta es estrictamente mejor por mas del margen.
        threshold = mae_base * (1.0 - margin)
        active = mae_meta < threshold
        return active, mae_base, mae_meta

    # ------------------------------------------------------------------
    # Tuning del meta (Opcion C: Optuna sobre las OOF del propio Stacked)
    # ------------------------------------------------------------------
    def _tune_meta(
        self,
        meta_features: np.ndarray,
        y_arr: np.ndarray,
        cat_indices: List[int],
        sample_weight: Optional[np.ndarray],
    ) -> Dict[str, object]:
        """Tunea (gam_n_splines, gam_lam) con TPE multivariado.

        Estructura mirror del nested CV del base:
          - Optuna study TPE multivariado, sin pruner (un score por trial).
          - Inner CV manual (fold a fold) para soportar sample_weight, igual
            que `_objective` en `tuning.py`.
          - Score: MAE promedio en val.
          - Robustez: si Optuna lanza, registramos warning y devolvemos
            los defaults pasados al __init__ (paridad con el path sin tuning).

        Devuelve un dict con `gam_n_splines`, `gam_lam` (best). Tambien
        publica `self.tuning_log_` con metricas para auditoria.
        """
        from src.step_04_train.search_spaces import suggest_gam_meta_params

        n = meta_features.shape[0]
        cv = KFold(
            n_splits=self.tune_meta_inner_folds,
            shuffle=True,
            random_state=self.random_state,
        )

        def _objective(trial: optuna.Trial) -> float:
            params = suggest_gam_meta_params(trial)
            scores: list[float] = []
            for tr_i, te_i in cv.split(meta_features):
                gam = get_gam_meta_model(
                    n_features=meta_features.shape[1],
                    cat_indices=cat_indices,
                    n_splines=params["gam_n_splines"],
                    lam=params["gam_lam"],
                    monotonic_pred_base=self.monotonic_pred_base,
                )
                sw_tr = index_or_none(sample_weight, tr_i)
                if sw_tr is not None:
                    gam.fit(meta_features[tr_i], y_arr[tr_i], weights=sw_tr)
                else:
                    gam.fit(meta_features[tr_i], y_arr[tr_i])
                pred = np.asarray(gam.predict(meta_features[te_i]), dtype=float)
                scores.append(float(mean_absolute_error(y_arr[te_i], pred)))
            return float(np.mean(scores))

        sampler = optuna.samplers.TPESampler(
            seed=self.random_state, multivariate=True,
            warn_independent_sampling=False,
        )
        study = optuna.create_study(direction="minimize", sampler=sampler)
        optuna.logging.set_verbosity(optuna.logging.WARNING)

        t0 = time.perf_counter()
        try:
            study.optimize(
                _objective,
                n_trials=self.tune_meta_trials,
                show_progress_bar=False,
                gc_after_trial=True,
            )
            best_params = dict(study.best_params)
            best_score = float(study.best_value)
            self.tuning_log_ = {
                "n_trials": self.tune_meta_trials,
                "inner_folds": self.tune_meta_inner_folds,
                "best_score_mae": best_score,
                "elapsed_s": round(time.perf_counter() - t0, 2),
                "fallback_used": False,
            }
            logger.info(
                f"StackedRegressor _tune_meta done | trials={self.tune_meta_trials} | "
                f"inner={self.tune_meta_inner_folds} | "
                f"best={{n_splines={best_params.get('gam_n_splines')}, "
                f"lam={best_params.get('gam_lam'):.4f}}} | "
                f"best_score_MAE={best_score:.4f} | "
                f"elapsed={self.tuning_log_['elapsed_s']}s"
            )
            return best_params
        except Exception:
            # Robustez: si Optuna falla por cualquier razon (memoria, GAM
            # degenerado, sample_weight invalido) caemos a defaults sin
            # romper el fit completo.
            logger.exception(
                "StackedRegressor _tune_meta FALLO; usando defaults "
                f"(n_splines={self.gam_n_splines}, lam={self.gam_lam})"
            )
            self.tuning_log_ = {
                "n_trials": self.tune_meta_trials,
                "inner_folds": self.tune_meta_inner_folds,
                "best_score_mae": float("nan"),
                "elapsed_s": round(time.perf_counter() - t0, 2),
                "fallback_used": True,
            }
            return {
                "gam_n_splines": self.gam_n_splines,
                "gam_lam": self.gam_lam,
            }

    # ------------------------------------------------------------------
    # Sklearn API
    # ------------------------------------------------------------------
    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        sample_weight: Optional[np.ndarray] = None,
    ) -> "StackedRegressor":
        if not isinstance(X, pd.DataFrame):
            raise TypeError(
                "StackedRegressor.fit espera X: pd.DataFrame (necesita "
                "acceder por nombre de columna a x_subset_cols)."
            )
        self._validate_x_subset(X)
        if self.n_oof_folds < 2:
            raise ValueError(
                f"n_oof_folds debe ser >=2 (recibido {self.n_oof_folds})"
            )

        n = len(X)
        oof_pred = np.full(n, np.nan, dtype=float)
        cv = KFold(
            n_splits=self.n_oof_folds,
            shuffle=True,
            random_state=self.random_state,
        )

        # Paso 1-2: cross_val_predict manual (sklearn.cross_val_predict no
        # splitea sample_weight por fold; lo trataria como kwarg estatico).
        for fold_idx, (tr_idx, te_idx) in enumerate(cv.split(X), start=1):
            base_fold = clone(self.base_pipeline)
            X_tr = X.iloc[tr_idx]
            y_tr = y.iloc[tr_idx]
            sw_tr = index_or_none(sample_weight, tr_idx)
            fit_with_optional_sample_weight(base_fold, X_tr, y_tr, sw_tr)
            oof_pred[te_idx] = base_fold.predict(X.iloc[te_idx])

        if np.isnan(oof_pred).any():
            n_nan = int(np.isnan(oof_pred).sum())
            raise RuntimeError(
                f"StackedRegressor: oof_pred tiene {n_nan} NaN tras CV. "
                "Revisa que el base_pipeline no produzca NaN."
            )

        # Paso 3: meta_features del subset (categoricas + flags + continuas).
        meta_features, names, cat_indices = self._build_meta_inputs(
            X, oof_pred, learn=True,
        )
        self.meta_feature_names_ = names
        self.n_meta_features_ = meta_features.shape[1]
        self.cat_indices_ = cat_indices

        # Paso 4: tuning opcional (Opcion C) + fit del GAM.
        y_arr = np.asarray(y, dtype=float)
        if self.tune_meta_trials > 0:
            best_meta_params = self._tune_meta(
                meta_features, y_arr, cat_indices, sample_weight,
            )
            n_splines_final = int(best_meta_params.get("gam_n_splines", self.gam_n_splines))
            lam_final = float(best_meta_params.get("gam_lam", self.gam_lam))
        else:
            n_splines_final = self.gam_n_splines
            lam_final = self.gam_lam
            self.tuning_log_: Dict[str, object] = {}  # marca que no hubo tuning

        self.tuned_meta_params_ = {
            "gam_n_splines": n_splines_final,
            "gam_lam": lam_final,
        }

        # Refit final del GAM con (best_params si hubo tuning, defaults si no).
        # Categoricas via cat_indices; constraint monotonic en s(pred_base)
        # (col 0) si esta activado y col 0 no es categorica.
        gam = get_gam_meta_model(
            n_features=self.n_meta_features_,
            cat_indices=cat_indices,
            n_splines=n_splines_final,
            lam=lam_final,
            monotonic_pred_base=self.monotonic_pred_base,
        )
        if sample_weight is not None:
            gam.fit(meta_features, y_arr, weights=sample_weight)
        else:
            gam.fit(meta_features, y_arr)
        self.meta_model_ = gam
        self.oof_pred_ = oof_pred

        # Paso 5: refit del base sobre TODO X (lo que se usa en inferencia).
        self.base_pipeline_ = clone(self.base_pipeline)
        fit_with_optional_sample_weight(self.base_pipeline_, X, y, sample_weight)

        # Paso 6: auto-fallback. El meta tiene que mejorar el MAE oof del
        # base por al menos `STACKING_FALLBACK_RELATIVE_MARGIN`. Si no, predict()
        # cae al base puro.
        meta_pred_oof = np.asarray(gam.predict(meta_features), dtype=float)
        if self.auto_fallback:
            active, mae_base, mae_meta = self._evaluate_fallback(
                y_arr, oof_pred, meta_pred_oof,
            )
            self._meta_active_ = active
            self._decision_log_: Dict[str, float] = {
                "mae_base_oof": mae_base,
                "mae_meta_oof": mae_meta,
                "delta_pct": ((mae_meta - mae_base) / mae_base * 100.0),
                "active": float(active),
            }
            decision = "ACTIVE" if active else "FALLBACK_TO_BASE"
            logger.info(
                f"StackedRegressor fallback check | base_mae={mae_base:.4f} | "
                f"meta_mae={mae_meta:.4f} | delta={self._decision_log_['delta_pct']:+.2f}% | "
                f"-> {decision}"
            )
        else:
            self._meta_active_ = True
            self._decision_log_ = {
                "mae_base_oof": float(mean_absolute_error(y_arr, oof_pred)),
                "mae_meta_oof": float(mean_absolute_error(y_arr, meta_pred_oof)),
                "delta_pct": 0.0,
                "active": 1.0,
            }

        cat_names = [names[i] for i in cat_indices]
        logger.info(
            f"StackedRegressor fit done | n={n} | oof_folds={self.n_oof_folds} | "
            f"meta_features={self.n_meta_features_} | "
            f"continuas={[n_ for n_ in names if n_ not in cat_names]} | "
            f"categoricas={cat_names}"
        )
        return self

    def predict(self, X) -> np.ndarray:
        if not isinstance(X, pd.DataFrame):
            raise TypeError(
                "StackedRegressor.predict espera X: pd.DataFrame "
                "(necesita acceder por nombre a x_subset_cols)."
            )
        if not hasattr(self, "meta_model_"):
            raise RuntimeError(
                "StackedRegressor no fue ajustado. Llama fit() primero."
            )
        self._validate_x_subset(X)

        pred_base = np.asarray(
            self.base_pipeline_.predict(X), dtype=float
        )

        # Auto-fallback: si el meta no mejoro al base en oof, no lo usamos.
        if not self._meta_active_:
            return pred_base

        meta_features, _, _ = self._build_meta_inputs(
            X, pred_base, learn=False,
        )
        return np.asarray(self.meta_model_.predict(meta_features), dtype=float)

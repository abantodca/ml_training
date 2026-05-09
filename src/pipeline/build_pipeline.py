"""Construccion del pipeline de preprocesamiento."""
from __future__ import annotations

from sklearn.feature_selection import VarianceThreshold
from sklearn.pipeline import Pipeline

from src.step_02_clean.imputers import CustomKNNImputer
from src.step_02_clean.missing_flags import MissingFlagger
from src.step_02_clean.outlier_score import LOFOutlierScorer
from src.step_02_clean.outliers import OutlierCapper
from src.step_03_features.feature_engineering import FeatureGenerator
from src.step_03_features.lag_features import LagFeatureTransformer


def create_preprocessing_pipeline() -> Pipeline:
    """Encadena: lags -> missing flags -> imputacion KNN -> capping -> LOF score -> ciclicas -> filtro varianza.

    Lag features (step 0): `LagFeatureTransformer` calcula rolling windows
    POR fold durante CV (sin leakage) y memoriza el historial para
    inferencia. En entrenamiento ve TODO el train fold; en cada predict()
    de test reusa solo el historial del fit, sin contaminar entre folds.

    `OutlierCapper(group_col="FUNDO")` aprende limites IQR/percentile POR
    FUNDO. Grupos con n<30 caen al cap global. Responde a fundos
    heterogeneos: capping global cortaria colas legitimas de un fundo
    bueno o no tocaria outliers reales de uno bajo.

    El `variance_filter` final descarta dummies constantes que aparecen
    cuando una variedad no observa todos los niveles de FUNDO/FORMATO
    (la dummy queda en 0 para todas las filas). `set_output('pandas')`
    preserva el DataFrame para mantener nombres de columna hacia XGB/LGB.

    Importante: el step `lag_features` requiere `y` en fit(); sklearn
    Pipeline lo propaga automaticamente cuando el caller hace `pipeline.fit(X, y)`.
    """
    return Pipeline(
        steps=[
            ("lag_features", LagFeatureTransformer()),
            ("missing_flags", MissingFlagger()),
            ("imputer", CustomKNNImputer()),
            ("outliers", OutlierCapper(group_col="FUNDO")),
            # LOF como FEATURE (additive). EDA POP 2026-05-09 detecto kurt=158
            # en DPC y 9.1% outliers IQR en KG/HA. LOF informa al modelo cuando
            # una fila es atipica multivariadamente — los arboles deciden si lo
            # usan o no. Va DESPUES del imputer (LOF no acepta NaN) y ANTES de
            # FeatureGenerator (asi el score se conserva en el output final).
            ("outlier_score", LOFOutlierScorer()),
            ("feature_engineering", FeatureGenerator()),
            (
                "variance_filter",
                VarianceThreshold(threshold=0.0).set_output(transform="pandas"),
            ),
        ]
    )

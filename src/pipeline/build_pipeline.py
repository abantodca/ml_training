"""Construccion del pipeline de preprocesamiento."""
from __future__ import annotations

from sklearn.pipeline import Pipeline

from src.step_02_clean.imputers import CustomKNNImputer
from src.step_02_clean.missing_flags import MissingFlagger
from src.step_02_clean.outliers import OutlierCapper
from src.step_03_features.feature_engineering import FeatureGenerator


def create_preprocessing_pipeline() -> Pipeline:
    """Encadena: missing flags -> imputacion KNN -> capping -> one-hot.

    Los lag features se calculan ANTES del pipeline, en `data_loader.py`.
    Esto deja la signature MLflow con 40 columnas (raw + lags) y traslada
    al backend la responsabilidad de reproducir el feature engineering en
    inferencia. La clase `LagFeatureTransformer` existe por si se decide a
    futuro encapsular los lags dentro del Pipeline (ver lag_features.py).
    """
    return Pipeline(
        steps=[
            ("missing_flags", MissingFlagger()),
            ("imputer", CustomKNNImputer()),
            ("outliers", OutlierCapper()),
            ("feature_engineering", FeatureGenerator()),
        ]
    )

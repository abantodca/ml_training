"""Construye features de equipo de cosechadores para enriquecer el modelo KG/JR_H.

Aislado del pipeline actual: lee la data por cosechador, calcula features a
nivel persona (con anti-leakage temporal), las agrega al grano del modelo
`(FECHA, FUNDO, FORMATO)` y las deja en Parquet listo para joinear con
`data/training/DB-HISTORICA.xlsx`.

Implementado con **polars** porque la data por cosechador escala a millones
de filas (1 fila por persona-dia). Polars hace rolling y groupby vectorizado
en paralelo; pandas se queda corto en memoria/CPU.

------------------------------------------------------------------------
SCHEMA REAL DEL CLIENTE (auto-resuelto sin --column-map)
------------------------------------------------------------------------
La data del cliente llega con estos nombres (lowercase) y se mapea solo:

    fecha          -> FECHA          (date / datetime)
    formato        -> FORMATO        (GRANEL / CLAMSHELL 4.4 OZ / ...)
    fundo          -> FUNDO          (A9, C5, ...)
    etapa          -> ETAPA          (PEAK, POST-PEAK, ...)
    campo          -> CAMPO          (lote dentro del fundo)
    variedad       -> VARIEDAD       (POP, ...)
    idcosechador   -> COSECHADOR_ID  (ID unico de la persona)
    id supervisor  -> ID_SUPERVISOR  (espacios -> '_'; auto-aliased)
    total_jabas    -> KG_PERSONA     (via --kg-per-jaba factor; default 1.0)
    total_horas    -> HORAS_PERSONA
    temporadas     -> TEMPORADAS     (años acumulados por persona)

`_normalize` aplica strip + UPPER + replace " " -> "_" y luego AUTO_ALIASES.
Para nombres que NO coincidan, usar --column-map 'origen=destino,...'.

------------------------------------------------------------------------
COLUMNAS: REQUERIDAS / AUTO-DERIVADAS / OPCIONALES
------------------------------------------------------------------------
Obligatorias (deben existir tras normalizacion):
    COSECHADOR_ID, FECHA, FUNDO, VARIEDAD

Auto-derivadas si faltan (no requieren input del cliente):
    DIA_COSECHA_PERSONA = dias desde el 1er dia de cosecha del año por persona
    BOLSA_FLAG          = top-quartile del KG/JR_H del año previo
                          (anti-leakage). Si !has_kg_horas -> default 0.

Opcionales (habilitan mas features cuando estan):
    FORMATO, ETAPA, CAMPO, GRUPO_ID,
    ID_SUPERVISOR  -> capa de management (track record + carga + continuidad)
    TEMPORADAS     -> tenure cross-year + flag veterano-bajo-baseline
    KG_PERSONA, HORAS_PERSONA -> rolling histories per persona

------------------------------------------------------------------------
SALIDA
------------------------------------------------------------------------
Parquet en `data/cosechadores/cosechador_features.parquet` con llave
`(FECHA, FUNDO, FORMATO)` mas las features agregadas. Si no hay FORMATO,
la llave es `(FECHA, FUNDO)` y se replica a todos los formatos al joinear.

------------------------------------------------------------------------
FEATURES GENERADAS (por bloques)
------------------------------------------------------------------------
A. Composicion del equipo:
    N_COSECHADORES, N_GRUPOS, GRUPO_SIZE_AVG, GRUPO_SIZE_STD

B. Tenure / experiencia (año + lifetime):
    TENURE_AVG, TENURE_MEDIAN, TENURE_STD, TENURE_MIN, TENURE_MAX,
    PCT_NOVATOS, PCT_VETERANOS,
    TENURE_LIFETIME_AVG, PCT_PRIMER_AÑO

C. Calidad del equipo:
    PCT_BOLSA,
    KG_JR_H_HIST_AVG_30D, KG_JR_H_HIST_STD_30D,
    PCT_TOP_HIST, PCT_BOTTOM_HIST,
    KG_JR_H_HIST_DOW_AVG (rolling 120d filtrado al mismo dia-de-semana)

D. Rotacion / churn:
    PCT_ROTADOS_30D, FUNDOS_VISITADOS_AVG_90D,
    PCT_NUEVOS_INGRESOS_14D, PCT_DESVINCULADOS_AYER,
    DIAS_DESDE_ULTIMO_TRABAJO_AVG,
    PCT_MISMA_DOTACION_AYER, PCT_CAMBIO_GRUPO

E. Heterogeneidad intra-grupo y jornada:
    KG_JR_H_GRUPO_CV_AVG, HORAS_STD_GRUPO_AVG,
    HORAS_HIST_DOW_AVG, PCT_JORNADA_PARCIAL

F. SUPERVISION (si ID_SUPERVISOR presente):
    N_SUPERVISORES,
    RATIO_COSECHADORES_POR_SUPERVISOR,
    EXCESO_GRUPO_FLAG          1 si ratio > 35 (regla operativa),
    SUP_CARGA_HOY_AVG          cosechadores totales que supervisa hoy,
    SUP_LOTES_HOY_AVG          (FUNDO,FORMATO) distintos en paralelo,
    SUP_KG_JR_H_HIST_AVG       rolling 30d del rendimiento de su equipo,
    SUP_KG_JR_H_HIST_STD       consistencia historica del supervisor,
    PCT_MISMO_SUPERVISOR_AYER  continuidad de gestion vs ayer.

G. CROSS-TEMPORADA + RESIDUAL + ANOMALIA:
    TEMPORADAS_AVG, TEMPORADAS_STD,
    PCT_TEMPORADA_INICIAL      % con TEMPORADAS == 1 (1er año cosechando),
    PCT_TEMPORADAS_VETERANO    % con TEMPORADAS >= 3,
    KG_JR_H_RESIDUAL_AVG       diff promedio vs propio baseline 30d,
    PCT_VETERANOS_BAJO_BASELINE  % del equipo con TEMPORADAS>=3 rindiendo
                                <80% de su propio baseline (capta casos 3 y 6:
                                veteranos buenos que bajan).

------------------------------------------------------------------------
COMO CADA FEATURE ATACA LOS CASOS DE NEGOCIO
------------------------------------------------------------------------
Caso 1 (kg/ha alto, rendimiento bajo: novatos + rotacion + supervisor):
    PCT_NOVATOS, PCT_ROTADOS_30D, PCT_NUEVOS_INGRESOS_14D,
    SUP_KG_JR_H_HIST_AVG, EXCESO_GRUPO_FLAG

Caso 2 (kg/ha alto, rendimiento alto: todos buenos):
    PCT_VETERANOS, KG_JR_H_HIST_AVG_30D, PCT_BOLSA,
    SUP_KG_JR_H_HIST_AVG (alto)

Caso 3 (veterano con campañas buenas y malas):
    TEMPORADAS_AVG, KG_JR_H_RESIDUAL_AVG, PCT_VETERANOS_BAJO_BASELINE

Caso 4 (novatos: dias de aprendizaje):
    PCT_NOVATOS, TENURE_AVG, KG_JR_H_HIST_AVG_30D

Caso 6 (personas buenas que bajan):
    KG_JR_H_RESIDUAL_AVG, PCT_VETERANOS_BAJO_BASELINE

Casos 7-8 (supervisor con varios grupos, max 35 cosechadores):
    SUP_LOTES_HOY_AVG, RATIO_COSECHADORES_POR_SUPERVISOR, EXCESO_GRUPO_FLAG

Anomalia de formato (kg/ha alto en GRANEL/CLAMSHELL no calza con el equipo):
    Combinacion PCT_NOVATOS + RATIO_COSECHADORES_POR_SUPERVISOR +
    SUP_KG_JR_H_HIST_AVG + PCT_BOLSA. El modelo aprende a discriminar
    "perfil de equipo vs formato esperado" y descuenta la expectativa
    cuando la composicion no cuadra con la regla operativa.

------------------------------------------------------------------------
CLI
------------------------------------------------------------------------
    # Generar sample TXT tab-separado con el schema REAL del cliente:
    python -m scripts.build_cosechador_features --make-sample

    # Procesar data real (auto-aliases + factor jabas->kg):
    python -m scripts.build_cosechador_features \\
        --input data/cosechadores/cosechadores.txt \\
        --output data/cosechadores/cosechador_features.parquet \\
        --kg-per-jaba 4.5

    # Validar schema + preview sin escribir:
    python -m scripts.build_cosechador_features \\
        --input data/cosechadores/cosechadores.txt --dry-run

    # Forzar separador si la autodeteccion falla:
    python -m scripts.build_cosechador_features --separator '\\t' ...

------------------------------------------------------------------------
COMO INTEGRAR AL PIPELINE (cuando el parquet exista)
------------------------------------------------------------------------
1) En `src/config.py`:

       COSECHADOR_FEATURES_FILE: Path = (
           DATA_DIR / "cosechadores" / "cosechador_features.parquet"
       )
       ENABLE_COSECHADOR_FEATURES: bool = _env_bool(
           "ENABLE_COSECHADOR_FEATURES", False,
       )
       COSECHADOR_NUMERIC_FEATURES: list[str] = [
           # Tier 1 (must-have)
           "KG_JR_H_HIST_AVG_30D", "PCT_BOLSA", "TENURE_AVG",
           # Tier 2 (strong supporting)
           "PCT_NOVATOS", "PCT_MISMA_DOTACION_AYER", "PCT_ROTADOS_30D",
           # Tier 3 (cross-year + intra-grupo)
           "TENURE_LIFETIME_AVG", "PCT_PRIMER_AÑO",
           "KG_JR_H_GRUPO_CV_AVG", "HORAS_STD_GRUPO_AVG",
           "PCT_JORNADA_PARCIAL", "PCT_CAMBIO_GRUPO",
           # Tier 4 (curva semanal a nivel cosechador)
           "KG_JR_H_HIST_DOW_AVG", "HORAS_HIST_DOW_AVG",
           # Tier 5 (supervision / management)
           "SUP_KG_JR_H_HIST_AVG", "PCT_MISMO_SUPERVISOR_AYER",
           "RATIO_COSECHADORES_POR_SUPERVISOR", "EXCESO_GRUPO_FLAG",
           "SUP_LOTES_HOY_AVG",
           # Tier 6 (cross-temporada + residual)
           "TEMPORADAS_AVG", "PCT_TEMPORADAS_VETERANO",
           "KG_JR_H_RESIDUAL_AVG", "PCT_VETERANOS_BAJO_BASELINE",
       ]

2) En `src/step_01_load/data_loader.py`, despues del read_excel del sheet:

       if ENABLE_COSECHADOR_FEATURES and COSECHADOR_FEATURES_FILE.exists():
           feats = pd.read_parquet(COSECHADOR_FEATURES_FILE)
           df = df.merge(feats, on=["FECHA", "FUNDO", "FORMATO"], how="left")

3) Extender NUMERIC_FEATURES en `config.py` con COSECHADOR_NUMERIC_FEATURES.

4) Smoke train con flag prendido:
       docker compose run --rm \\
           -e ENABLE_COSECHADOR_FEATURES=1 \\
           trainer --varieties POP --tuning smoke
   Comparar MAPE_oof y gap contra baseline (LGB v3: 14.86% / 0.138).

------------------------------------------------------------------------
LEAKAGE TEMPORAL
------------------------------------------------------------------------
Todas las features de "track record" (KG_JR_H_HIST_*, BOLSA_FLAG derivado,
SUP_KG_JR_H_HIST_*, ROTADO_30D) usan info ESTRICTAMENTE anterior a FECHA
(closed="left" en rolling). Si una persona/supervisor no tiene historial
todavia, la fila queda con NaN y el imputer del pipeline la maneja.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema y umbrales
# ---------------------------------------------------------------------------
REQUIRED_COLS: tuple[str, ...] = (
    "COSECHADOR_ID",
    "FECHA",
    "FUNDO",
    "VARIEDAD",
)
# Se auto-derivan en _derive_missing_inputs si no estan en la data.
AUTO_DERIVED_COLS: tuple[str, ...] = (
    "DIA_COSECHA_PERSONA",
    "BOLSA_FLAG",
)
OPTIONAL_COLS: tuple[str, ...] = (
    "FORMATO",
    "ETAPA",
    "CAMPO",
    "GRUPO_ID",
    "ID_SUPERVISOR",
    "TEMPORADAS",
    "KG_PERSONA",
    "HORAS_PERSONA",
)

# Alias auto-resueltos en _normalize (input lower-case -> nombre esperado).
# Match el schema real del cliente para no requerir --column-map manual.
AUTO_ALIASES: dict[str, str] = {
    "IDCOSECHADOR": "COSECHADOR_ID",
    "ID_COSECHADOR": "COSECHADOR_ID",
    "DNI": "COSECHADOR_ID",
    "IDSUPERVISOR": "ID_SUPERVISOR",
    "ID_SUP": "ID_SUPERVISOR",
    "TOTAL_HORAS": "HORAS_PERSONA",
    # TOTAL_JABAS NO se aliasea aqui: se convierte a KG_PERSONA en
    # _apply_kg_jaba_conversion para permitir factor via --kg-per-jaba.
}

# Umbrales (dias acumulados de cosecha del año).
NOVATO_THRESHOLD_DAYS: int = 30
VETERANO_THRESHOLD_DAYS: int = 180

# Umbral de temporadas (años) para considerar a alguien "veterano" cross-year.
VETERANO_TEMPORADAS: int = 3

# Regla operativa: maximo 35 cosechadores por supervisor.
MAX_COSECHADORES_POR_SUPERVISOR: int = 35

# Threshold "veterano bajo baseline": rendimiento_hoy < ratio * baseline_propio.
BAJO_BASELINE_RATIO: float = 0.8

# Ventanas rolling (en dias calendario).
ROLLING_HIST_DAYS: int = 30
ROTACION_WINDOW_DAYS: int = 30
FUNDOS_WINDOW_DAYS: int = 90
NUEVO_INGRESO_WINDOW_DAYS: int = 14
# 120d ~ 17 ocurrencias por dia-de-semana, suficiente para mean estable.
ROLLING_DOW_DAYS: int = 120
JORNADA_PARCIAL_THRESHOLD: float = 6.0

DEFAULT_OUTPUT: Path = Path("data/cosechadores/cosechador_features.parquet")


@dataclass
class BuildContext:
    has_formato: bool
    has_grupo: bool
    has_supervisor: bool
    has_temporadas: bool
    has_kg_horas: bool  # KG_PERSONA + HORAS_PERSONA (ambos)
    has_horas: bool  # HORAS_PERSONA (puede ir solo)


def _build_context(df: pl.DataFrame) -> BuildContext:
    has_horas = "HORAS_PERSONA" in df.columns
    return BuildContext(
        has_formato="FORMATO" in df.columns,
        has_grupo="GRUPO_ID" in df.columns,
        has_supervisor="ID_SUPERVISOR" in df.columns,
        has_temporadas="TEMPORADAS" in df.columns,
        has_kg_horas=("KG_PERSONA" in df.columns) and has_horas,
        has_horas=has_horas,
    )


# ---------------------------------------------------------------------------
# Validacion de schema
# ---------------------------------------------------------------------------
def validate_schema(df: pl.DataFrame) -> BuildContext:
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Columnas requeridas faltantes: {missing}. Disponibles: {df.columns}"
        )
    ctx = _build_context(df)
    n_personas = df.select(pl.col("COSECHADOR_ID").n_unique()).item()
    logger.info("Schema OK. Filas=%d, cosechadores=%d", df.height, n_personas)
    logger.info(
        "FORMATO=%s GRUPO_ID=%s SUPERVISOR=%s TEMPORADAS=%s HORAS=%s KG/HORAS=%s",
        "si" if ctx.has_formato else "no",
        "si" if ctx.has_grupo else "no",
        "si" if ctx.has_supervisor else "no",
        "si" if ctx.has_temporadas else "no",
        "si" if ctx.has_horas else "no",
        "si" if ctx.has_kg_horas else "no",
    )
    return ctx


# ---------------------------------------------------------------------------
# Auto-derivacion de columnas que solian ser requeridas
# ---------------------------------------------------------------------------
def _derive_missing_inputs(df: pl.DataFrame) -> pl.DataFrame:
    """Deriva DIA_COSECHA_PERSONA y BOLSA_FLAG si no estan presentes.

    El schema real del cliente no incluye estas dos columnas, asi que el
    script las computa solo. Si la data las trae, se respetan tal cual.
    """
    ctx = _build_context(df)

    if "DIA_COSECHA_PERSONA" not in df.columns:
        df = df.with_columns(
            pl.col("FECHA")
            .min()
            .over(["COSECHADOR_ID", pl.col("FECHA").dt.year()])
            .alias("__primer_dia_anio"),
        )
        df = df.with_columns(
            (pl.col("FECHA") - pl.col("__primer_dia_anio"))
            .dt.total_days()
            .cast(pl.Int64)
            .alias("DIA_COSECHA_PERSONA"),
        ).drop("__primer_dia_anio")
        logger.info("DIA_COSECHA_PERSONA derivada de FECHA + COSECHADOR_ID.")

    if "BOLSA_FLAG" not in df.columns:
        if ctx.has_kg_horas:
            tmp = df.with_columns(
                pl.col("FECHA").dt.year().alias("__y"),
                pl.when(pl.col("HORAS_PERSONA") > 0)
                .then(pl.col("KG_PERSONA") / pl.col("HORAS_PERSONA"))
                .otherwise(None)
                .alias("__kgh"),
            )
            per_anio = tmp.group_by(["COSECHADOR_ID", "__y"]).agg(
                pl.col("__kgh").mean().alias("__kgh_anio"),
            )
            cuts = per_anio.group_by("__y").agg(
                pl.col("__kgh_anio").quantile(0.75).alias("__q75"),
            )
            bolsa = (
                per_anio.join(cuts, on="__y", how="left")
                .with_columns(
                    (pl.col("__kgh_anio") >= pl.col("__q75"))
                    .cast(pl.Int8)
                    .alias("__bolsa"),
                    (pl.col("__y") + 1).alias("__y_next"),
                )
                .select(
                    pl.col("COSECHADOR_ID"),
                    pl.col("__y_next").alias("__y"),
                    pl.col("__bolsa").alias("BOLSA_FLAG"),
                )
            )
            df = df.with_columns(pl.col("FECHA").dt.year().alias("__y"))
            df = df.join(bolsa, on=["COSECHADOR_ID", "__y"], how="left").drop("__y")
            df = df.with_columns(pl.col("BOLSA_FLAG").fill_null(0))
            logger.info("BOLSA_FLAG derivado de top-quartile KG/JR_H año previo.")
        else:
            df = df.with_columns(pl.lit(0, dtype=pl.Int8).alias("BOLSA_FLAG"))
            logger.warning(
                "BOLSA_FLAG ausente y no hay KG/HORAS: default 0 (feature constante).",
            )
    return df


# ---------------------------------------------------------------------------
# Features a nivel persona (con anti-leakage temporal)
# ---------------------------------------------------------------------------
def _person_track_record(df: pl.DataFrame, ctx: BuildContext) -> pl.DataFrame:
    """Track record por persona-fecha. Todo rolling con closed="left"."""
    df = df.sort(["COSECHADOR_ID", "FECHA"])

    if ctx.has_kg_horas:
        df = df.with_columns(
            pl.when(pl.col("HORAS_PERSONA") == 0)
            .then(None)
            .otherwise(pl.col("KG_PERSONA") / pl.col("HORAS_PERSONA"))
            .alias("KG_JR_H_PERSONA"),
        )
    else:
        df = df.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("KG_JR_H_PERSONA"),
        )

    df = df.with_columns(
        pl.col("KG_JR_H_PERSONA")
        .rolling_mean_by("FECHA", window_size=f"{ROLLING_HIST_DAYS}d", closed="left")
        .over("COSECHADOR_ID")
        .alias("KG_JR_H_HIST_AVG_30D"),
        pl.col("KG_JR_H_PERSONA")
        .rolling_std_by("FECHA", window_size=f"{ROLLING_HIST_DAYS}d", closed="left")
        .over("COSECHADOR_ID")
        .alias("KG_JR_H_HIST_STD_30D"),
    )

    df = df.with_columns(
        (pl.col("FECHA") - pl.col("FECHA").shift(1).over("COSECHADOR_ID"))
        .dt.total_days()
        .cast(pl.Float64)
        .alias("DIAS_DESDE_ULTIMO"),
    )

    # Rolling sobre FUNDO (string): df.rolling().agg() devuelve 1 fila por
    # fila de input en el mismo orden -> hstack directo.
    rolled_30 = (
        df.rolling(
            index_column="FECHA",
            period=f"{ROTACION_WINDOW_DAYS}d",
            closed="left",
            group_by="COSECHADOR_ID",
        )
        .agg(pl.col("FUNDO").unique().alias("__fundos_30d"))
        .select("__fundos_30d")
    )
    rolled_90 = (
        df.rolling(
            index_column="FECHA",
            period=f"{FUNDOS_WINDOW_DAYS}d",
            closed="left",
            group_by="COSECHADOR_ID",
        )
        .agg(
            pl.col("FUNDO").n_unique().alias("__nfundos_90d"),
            pl.col("FUNDO").len().alias("__count_90d"),
        )
        .select(["__nfundos_90d", "__count_90d"])
    )
    df = df.hstack(rolled_30).hstack(rolled_90)

    df = df.with_columns(
        pl.when(pl.col("__fundos_30d").list.len() == 0)
        .then(pl.lit(0.0))
        .when(
            (pl.col("__fundos_30d").list.len() == 1)
            & pl.col("__fundos_30d").list.contains(pl.col("FUNDO"))
        )
        .then(pl.lit(0.0))
        .otherwise(pl.lit(1.0))
        .alias("ROTADO_30D"),
        pl.when(pl.col("__count_90d") == 0)
        .then(None)
        .otherwise(pl.col("__nfundos_90d").cast(pl.Float64))
        .alias("FUNDOS_VISITADOS_90D"),
    ).drop(["__fundos_30d", "__nfundos_90d", "__count_90d"])

    # Primer dia del año por persona (para PCT_NUEVOS_INGRESOS_14D).
    df = df.with_columns(
        pl.col("FECHA")
        .min()
        .over(["COSECHADOR_ID", pl.col("FECHA").dt.year()])
        .alias("PRIMER_DIA_ANIO"),
    )

    # TENURE_LIFETIME: dias unicos de cosecha en toda la historia disponible.
    # Complementa a TEMPORADAS (si existe) pero no la reemplaza.
    unique_dates = (
        df.select(["COSECHADOR_ID", "FECHA"])
        .unique()
        .sort(["COSECHADOR_ID", "FECHA"])
        .with_columns(
            (pl.col("FECHA").cum_count().over("COSECHADOR_ID") - 1)
            .cast(pl.Float64)
            .alias("TENURE_LIFETIME"),
        )
    )
    df = df.join(unique_dates, on=["COSECHADOR_ID", "FECHA"], how="left")

    primer_anio = df.group_by("COSECHADOR_ID").agg(
        pl.col("FECHA").dt.year().min().alias("__primer_anio_persona"),
    )
    df = (
        df.join(primer_anio, on="COSECHADOR_ID", how="left")
        .with_columns(
            (pl.col("FECHA").dt.year() == pl.col("__primer_anio_persona"))
            .cast(pl.Float64)
            .alias("ES_PRIMER_AÑO"),
        )
        .drop("__primer_anio_persona")
    )

    # Rolling DOW (curva semanal a nivel cosechador).
    if ctx.has_kg_horas or ctx.has_horas:
        df = df.with_columns(pl.col("FECHA").dt.weekday().alias("__DOW"))
        df = df.sort(["COSECHADOR_ID", "__DOW", "FECHA"])
        dow_cols: list[pl.Expr] = []
        if ctx.has_kg_horas:
            dow_cols.append(
                pl.col("KG_JR_H_PERSONA")
                .rolling_mean_by(
                    "FECHA",
                    window_size=f"{ROLLING_DOW_DAYS}d",
                    closed="left",
                )
                .over(["COSECHADOR_ID", "__DOW"])
                .alias("KG_JR_H_HIST_DOW_AVG_120D"),
            )
        if ctx.has_horas:
            dow_cols.append(
                pl.col("HORAS_PERSONA")
                .rolling_mean_by(
                    "FECHA",
                    window_size=f"{ROLLING_DOW_DAYS}d",
                    closed="left",
                )
                .over(["COSECHADOR_ID", "__DOW"])
                .alias("HORAS_HIST_DOW_AVG_120D"),
            )
        df = df.with_columns(dow_cols)
        df = df.sort(["COSECHADOR_ID", "FECHA"]).drop("__DOW")

    # Residual personal: rendimiento_hoy - propio baseline 30d.
    # Veterano_bajo_baseline: capta caso 3 y 6 (veteranos buenos que bajan).
    if ctx.has_kg_horas:
        df = df.with_columns(
            (pl.col("KG_JR_H_PERSONA") - pl.col("KG_JR_H_HIST_AVG_30D"))
            .alias("KG_JR_H_RESIDUAL"),
        )
        if ctx.has_temporadas:
            df = df.with_columns(
                (
                    (pl.col("TEMPORADAS") >= VETERANO_TEMPORADAS)
                    & (pl.col("KG_JR_H_HIST_AVG_30D") > 0)
                    & (
                        pl.col("KG_JR_H_PERSONA")
                        < BAJO_BASELINE_RATIO * pl.col("KG_JR_H_HIST_AVG_30D")
                    )
                )
                .cast(pl.Float64)
                .alias("VETERANO_BAJO_BASELINE"),
            )
    return df


def _person_quartile_flags(df: pl.DataFrame) -> pl.DataFrame:
    """Marca top/bottom quartile usando cuartiles del año anterior (anti-leakage)."""
    df = df.with_columns(pl.col("FECHA").dt.year().alias("ANIO"))
    quartiles = (
        df.filter(pl.col("KG_JR_H_HIST_AVG_30D").is_not_null())
        .group_by("ANIO")
        .agg(
            pl.col("KG_JR_H_HIST_AVG_30D").quantile(0.25).alias("Q1"),
            pl.col("KG_JR_H_HIST_AVG_30D").quantile(0.75).alias("Q3"),
        )
        .with_columns((pl.col("ANIO") + 1).alias("ANIO"))
    )
    df = df.join(quartiles, on="ANIO", how="left")
    df = df.with_columns(
        (pl.col("KG_JR_H_HIST_AVG_30D") >= pl.col("Q3"))
        .cast(pl.Float64)
        .alias("TOP_HIST"),
        (pl.col("KG_JR_H_HIST_AVG_30D") <= pl.col("Q1"))
        .cast(pl.Float64)
        .alias("BOTTOM_HIST"),
    ).drop(["Q1", "Q3", "ANIO"])
    return df


# ---------------------------------------------------------------------------
# Continuidad de grupo
# ---------------------------------------------------------------------------
def _add_group_continuity(df: pl.DataFrame, ctx: BuildContext) -> pl.DataFrame:
    """CAMBIO_GRUPO=1 si la persona cambio de GRUPO_ID vs ayer mismo lote."""
    if not ctx.has_grupo:
        return df.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("CAMBIO_GRUPO"),
        )

    join_keys = ["COSECHADOR_ID", "FECHA", "FUNDO"]
    if ctx.has_formato:
        join_keys.append("FORMATO")
    select_cols = ["COSECHADOR_ID", "FECHA", "FUNDO"]
    if ctx.has_formato:
        select_cols.append("FORMATO")
    select_cols.append("GRUPO_ID")

    ayer = (
        df.select(select_cols)
        .with_columns((pl.col("FECHA") + pl.duration(days=1)).alias("FECHA"))
        .rename({"GRUPO_ID": "__grupo_ayer"})
    )
    df = df.join(ayer, on=join_keys, how="left")
    df = df.with_columns(
        pl.when(pl.col("__grupo_ayer").is_null())
        .then(None)
        .otherwise((pl.col("GRUPO_ID") != pl.col("__grupo_ayer")).cast(pl.Float64))
        .alias("CAMBIO_GRUPO"),
    ).drop("__grupo_ayer")
    return df


# ---------------------------------------------------------------------------
# Track record + continuidad a nivel SUPERVISOR (capa de management)
# ---------------------------------------------------------------------------
def _supervisor_track_record(df: pl.DataFrame, ctx: BuildContext) -> pl.DataFrame:
    """Carga, lotes en paralelo y rolling 30d del rendimiento del supervisor."""
    if not ctx.has_supervisor:
        return df

    if ctx.has_formato:
        lote_expr = pl.concat_str(["FUNDO", "FORMATO"], separator="|").n_unique()
    else:
        lote_expr = pl.col("FUNDO").n_unique()

    sup_load = df.group_by(["ID_SUPERVISOR", "FECHA"]).agg(
        pl.col("COSECHADOR_ID").n_unique().cast(pl.Float64).alias("SUP_CARGA_HOY"),
        lote_expr.cast(pl.Float64).alias("SUP_LOTES_HOY"),
    )
    df = df.join(sup_load, on=["ID_SUPERVISOR", "FECHA"], how="left")

    if ctx.has_kg_horas:
        sup_daily = (
            df.group_by(["ID_SUPERVISOR", "FECHA"])
            .agg(pl.col("KG_JR_H_PERSONA").mean().alias("__sup_kgh_day"))
            .sort(["ID_SUPERVISOR", "FECHA"])
            .with_columns(
                pl.col("__sup_kgh_day")
                .rolling_mean_by(
                    "FECHA",
                    window_size=f"{ROLLING_HIST_DAYS}d",
                    closed="left",
                )
                .over("ID_SUPERVISOR")
                .alias("SUP_KG_JR_H_HIST_AVG_30D"),
                pl.col("__sup_kgh_day")
                .rolling_std_by(
                    "FECHA",
                    window_size=f"{ROLLING_HIST_DAYS}d",
                    closed="left",
                )
                .over("ID_SUPERVISOR")
                .alias("SUP_KG_JR_H_HIST_STD_30D"),
            )
            .select(
                "ID_SUPERVISOR",
                "FECHA",
                "SUP_KG_JR_H_HIST_AVG_30D",
                "SUP_KG_JR_H_HIST_STD_30D",
            )
        )
        df = df.join(sup_daily, on=["ID_SUPERVISOR", "FECHA"], how="left")
    return df


def _add_supervisor_continuity(df: pl.DataFrame, ctx: BuildContext) -> pl.DataFrame:
    """MISMO_SUPERVISOR_AYER por persona-dia.
    1 = mismo supervisor que ayer, 0 = cambio, NaN = no trabajo ayer.
    Asume 1 supervisor por persona-dia; dedupe defensivo si hay duplicados.
    """
    if not ctx.has_supervisor:
        return df.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("MISMO_SUPERVISOR_AYER"),
        )

    ayer = (
        df.select(["COSECHADOR_ID", "FECHA", "ID_SUPERVISOR"])
        .unique(subset=["COSECHADOR_ID", "FECHA"])
        .with_columns((pl.col("FECHA") + pl.duration(days=1)).alias("FECHA"))
        .rename({"ID_SUPERVISOR": "__sup_ayer"})
    )
    df = df.join(ayer, on=["COSECHADOR_ID", "FECHA"], how="left")
    df = df.with_columns(
        pl.when(pl.col("__sup_ayer").is_null())
        .then(None)
        .otherwise(
            (pl.col("ID_SUPERVISOR") == pl.col("__sup_ayer")).cast(pl.Float64)
        )
        .alias("MISMO_SUPERVISOR_AYER"),
    ).drop("__sup_ayer")
    return df


# ---------------------------------------------------------------------------
# Agregacion al grano del modelo: (FECHA, FUNDO, [FORMATO])
# ---------------------------------------------------------------------------
def _aggregate_to_lot(df: pl.DataFrame, ctx: BuildContext) -> pl.DataFrame:
    keys: list[str] = ["FECHA", "FUNDO"]
    if ctx.has_formato:
        keys.append("FORMATO")

    df = df.with_columns(
        (
            (pl.col("FECHA") - pl.col("PRIMER_DIA_ANIO")).dt.total_days()
            <= NUEVO_INGRESO_WINDOW_DAYS
        )
        .cast(pl.Float64)
        .alias("__nuevo_ingreso_14d"),
    )

    aggs: list[pl.Expr] = [
        # A. Composicion
        pl.col("COSECHADOR_ID").n_unique().alias("N_COSECHADORES"),
        # B. Tenure
        pl.col("DIA_COSECHA_PERSONA").mean().alias("TENURE_AVG"),
        pl.col("DIA_COSECHA_PERSONA").median().alias("TENURE_MEDIAN"),
        pl.col("DIA_COSECHA_PERSONA").std().alias("TENURE_STD"),
        pl.col("DIA_COSECHA_PERSONA").min().alias("TENURE_MIN"),
        pl.col("DIA_COSECHA_PERSONA").max().alias("TENURE_MAX"),
        (pl.col("DIA_COSECHA_PERSONA") < NOVATO_THRESHOLD_DAYS)
        .cast(pl.Float64)
        .mean()
        .alias("PCT_NOVATOS"),
        (pl.col("DIA_COSECHA_PERSONA") > VETERANO_THRESHOLD_DAYS)
        .cast(pl.Float64)
        .mean()
        .alias("PCT_VETERANOS"),
        pl.col("TENURE_LIFETIME").mean().alias("TENURE_LIFETIME_AVG"),
        pl.col("ES_PRIMER_AÑO").mean().alias("PCT_PRIMER_AÑO"),
        # C. Calidad
        pl.col("BOLSA_FLAG").cast(pl.Float64).mean().alias("PCT_BOLSA"),
        pl.col("KG_JR_H_HIST_AVG_30D").mean().alias("KG_JR_H_HIST_AVG_30D"),
        pl.col("KG_JR_H_HIST_STD_30D").mean().alias("KG_JR_H_HIST_STD_30D"),
        pl.col("TOP_HIST").mean().alias("PCT_TOP_HIST"),
        pl.col("BOTTOM_HIST").mean().alias("PCT_BOTTOM_HIST"),
        # D. Rotacion / churn
        pl.col("ROTADO_30D").mean().alias("PCT_ROTADOS_30D"),
        pl.col("FUNDOS_VISITADOS_90D").mean().alias("FUNDOS_VISITADOS_AVG_90D"),
        pl.col("__nuevo_ingreso_14d").mean().alias("PCT_NUEVOS_INGRESOS_14D"),
        pl.col("DIAS_DESDE_ULTIMO").mean().alias("DIAS_DESDE_ULTIMO_TRABAJO_AVG"),
        pl.col("CAMBIO_GRUPO").mean().alias("PCT_CAMBIO_GRUPO"),
    ]

    if ctx.has_kg_horas:
        aggs.append(
            pl.col("KG_JR_H_HIST_DOW_AVG_120D").mean().alias("KG_JR_H_HIST_DOW_AVG"),
        )
        aggs.append(
            pl.col("KG_JR_H_RESIDUAL").mean().alias("KG_JR_H_RESIDUAL_AVG"),
        )
    if ctx.has_horas:
        aggs.extend(
            [
                pl.col("HORAS_HIST_DOW_AVG_120D").mean().alias("HORAS_HIST_DOW_AVG"),
                (pl.col("HORAS_PERSONA") < JORNADA_PARCIAL_THRESHOLD)
                .cast(pl.Float64)
                .mean()
                .alias("PCT_JORNADA_PARCIAL"),
            ]
        )

    # F. Supervision
    if ctx.has_supervisor:
        aggs.extend(
            [
                pl.col("ID_SUPERVISOR").n_unique().alias("N_SUPERVISORES"),
                pl.col("SUP_CARGA_HOY").mean().alias("SUP_CARGA_HOY_AVG"),
                pl.col("SUP_LOTES_HOY").mean().alias("SUP_LOTES_HOY_AVG"),
                pl.col("MISMO_SUPERVISOR_AYER").mean().alias("PCT_MISMO_SUPERVISOR_AYER"),
            ]
        )
        if ctx.has_kg_horas:
            aggs.extend(
                [
                    pl.col("SUP_KG_JR_H_HIST_AVG_30D").mean().alias("SUP_KG_JR_H_HIST_AVG"),
                    pl.col("SUP_KG_JR_H_HIST_STD_30D").mean().alias("SUP_KG_JR_H_HIST_STD"),
                ]
            )

    # G. Cross-temporada
    if ctx.has_temporadas:
        aggs.extend(
            [
                pl.col("TEMPORADAS").cast(pl.Float64).mean().alias("TEMPORADAS_AVG"),
                pl.col("TEMPORADAS").cast(pl.Float64).std().alias("TEMPORADAS_STD"),
                (pl.col("TEMPORADAS") == 1)
                .cast(pl.Float64)
                .mean()
                .alias("PCT_TEMPORADA_INICIAL"),
                (pl.col("TEMPORADAS") >= VETERANO_TEMPORADAS)
                .cast(pl.Float64)
                .mean()
                .alias("PCT_TEMPORADAS_VETERANO"),
            ]
        )
        if ctx.has_kg_horas:
            aggs.append(
                pl.col("VETERANO_BAJO_BASELINE")
                .mean()
                .alias("PCT_VETERANOS_BAJO_BASELINE"),
            )

    out = df.group_by(keys).agg(aggs)

    # Anomalia de gestion (regla operativa: max 35 cosechadores/supervisor).
    if ctx.has_supervisor:
        out = out.with_columns(
            pl.when(pl.col("N_SUPERVISORES") > 0)
            .then(
                pl.col("N_COSECHADORES").cast(pl.Float64)
                / pl.col("N_SUPERVISORES").cast(pl.Float64)
            )
            .otherwise(None)
            .alias("RATIO_COSECHADORES_POR_SUPERVISOR"),
        )
        out = out.with_columns(
            (
                pl.col("RATIO_COSECHADORES_POR_SUPERVISOR")
                > MAX_COSECHADORES_POR_SUPERVISOR
            )
            .cast(pl.Float64)
            .alias("EXCESO_GRUPO_FLAG"),
        )

    # Composicion intra-grupo (si has_grupo).
    if ctx.has_grupo:
        gsize = df.group_by(keys + ["GRUPO_ID"]).len().rename({"len": "size"})
        gstats = gsize.group_by(keys).agg(
            pl.col("GRUPO_ID").n_unique().alias("N_GRUPOS"),
            pl.col("size").mean().cast(pl.Float64).alias("GRUPO_SIZE_AVG"),
            pl.col("size").std().alias("GRUPO_SIZE_STD"),
        )
        out = out.join(gstats, on=keys, how="left")

        intra_aggs: list[pl.Expr] = []
        if ctx.has_kg_horas:
            intra_aggs.extend(
                [
                    pl.col("KG_JR_H_PERSONA").std().alias("__sd_kg"),
                    pl.col("KG_JR_H_PERSONA").mean().alias("__mu_kg"),
                ]
            )
        if ctx.has_horas:
            intra_aggs.append(pl.col("HORAS_PERSONA").std().alias("__sd_h"))
        if intra_aggs:
            grupo_intra = df.group_by(keys + ["GRUPO_ID"]).agg(intra_aggs)
            if ctx.has_kg_horas:
                grupo_intra = grupo_intra.with_columns(
                    pl.when(pl.col("__mu_kg") > 0)
                    .then(pl.col("__sd_kg") / pl.col("__mu_kg"))
                    .otherwise(None)
                    .alias("__cv_kg"),
                )
            lot_intra_aggs: list[pl.Expr] = []
            if ctx.has_kg_horas:
                lot_intra_aggs.append(
                    pl.col("__cv_kg").mean().alias("KG_JR_H_GRUPO_CV_AVG"),
                )
            if ctx.has_horas:
                lot_intra_aggs.append(
                    pl.col("__sd_h").mean().alias("HORAS_STD_GRUPO_AVG"),
                )
            lot_intra = grupo_intra.group_by(keys).agg(lot_intra_aggs)
            out = out.join(lot_intra, on=keys, how="left")

    out = _add_continuity_features(out, df, keys)
    return out.sort(keys)


def _add_continuity_features(
    feats: pl.DataFrame,
    raw: pl.DataFrame,
    keys: list[str],
) -> pl.DataFrame:
    """PCT_MISMA_DOTACION_AYER y PCT_DESVINCULADOS_AYER por lote."""
    sets = raw.group_by(keys).agg(
        pl.col("COSECHADOR_ID").unique().sort().alias("set_hoy"),
    )
    ayer = sets.with_columns(
        (pl.col("FECHA") + pl.duration(days=1)).alias("FECHA"),
    ).rename({"set_hoy": "set_ayer"})
    sets = sets.join(ayer, on=keys, how="left")

    sets = sets.with_columns(
        pl.col("set_hoy")
        .list.set_intersection(pl.col("set_ayer"))
        .list.len()
        .cast(pl.Float64)
        .alias("__inter"),
        pl.col("set_hoy").list.len().cast(pl.Float64).alias("__n_hoy"),
        pl.col("set_ayer").list.len().cast(pl.Float64).alias("__n_ayer"),
    )
    has_ayer = pl.col("set_ayer").is_not_null()
    sets = sets.with_columns(
        pl.when(has_ayer & (pl.col("__n_hoy") > 0))
        .then(pl.col("__inter") / pl.col("__n_hoy"))
        .otherwise(None)
        .alias("PCT_MISMA_DOTACION_AYER"),
        pl.when(has_ayer & (pl.col("__n_ayer") > 0))
        .then((pl.col("__n_ayer") - pl.col("__inter")) / pl.col("__n_ayer"))
        .otherwise(None)
        .alias("PCT_DESVINCULADOS_AYER"),
    )
    return feats.join(
        sets.select(keys + ["PCT_MISMA_DOTACION_AYER", "PCT_DESVINCULADOS_AYER"]),
        on=keys,
        how="left",
    )


# ---------------------------------------------------------------------------
# IO + normalizacion
# ---------------------------------------------------------------------------
def _sniff_separator(input_path: Path) -> str:
    with open(input_path, "r", encoding="utf-8", errors="replace") as f:
        header = f.readline()
    counts = {
        "\t": header.count("\t"),
        ",": header.count(","),
        "|": header.count("|"),
        ";": header.count(";"),
    }
    sep, n = max(counts.items(), key=lambda kv: kv[1])
    if n == 0:
        raise ValueError(
            f"No pude detectar separador en {input_path}. "
            f"Pasa --separator explicito (ej: '\\t')."
        )
    logger.info("Separador detectado: %r (%d ocurrencias en header)", sep, n)
    return sep


def _read_input(input_path: Path, separator: str | None = None) -> pl.DataFrame:
    suffix = input_path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return pl.read_excel(input_path)
    if suffix == ".csv":
        sep = separator or ","
        return pl.read_csv(input_path, separator=sep, try_parse_dates=True)
    if suffix in (".txt", ".tsv"):
        sep = separator or _sniff_separator(input_path)
        return pl.read_csv(input_path, separator=sep, try_parse_dates=True)
    if suffix == ".parquet":
        return pl.read_parquet(input_path)
    raise ValueError(f"Formato no soportado: {input_path.suffix}")


def _normalize(df: pl.DataFrame) -> pl.DataFrame:
    """strip + UPPER + replace ' '->'_' en headers + AUTO_ALIASES.
    Cast de FECHA a Datetime para que rolling_*_by funcione.
    """
    renames: dict[str, str] = {}
    for c in df.columns:
        clean = c.strip().upper().replace(" ", "_")
        clean = AUTO_ALIASES.get(clean, clean)
        if clean != c:
            renames[c] = clean
    if renames:
        logger.info("Normalize columnas: %s", renames)
        df = df.rename(renames)

    if "FECHA" in df.columns:
        dtype = df.schema["FECHA"]
        if dtype == pl.Date:
            df = df.with_columns(pl.col("FECHA").cast(pl.Datetime("us")))
        elif dtype == pl.String or dtype == pl.Utf8:
            df = df.with_columns(pl.col("FECHA").str.to_datetime())
        elif not isinstance(dtype, pl.Datetime):
            df = df.with_columns(pl.col("FECHA").cast(pl.Datetime("us")))
    return df


def _parse_column_map(spec: str | None) -> dict[str, str]:
    """Parsea 'A=B,C=D' a {'A': 'B', 'C': 'D'}."""
    if not spec:
        return {}
    out: dict[str, str] = {}
    for pair in spec.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"Mapping invalido: {pair!r} (esperado 'src=dst')")
        src, dst = pair.split("=", 1)
        out[src.strip()] = dst.strip()
    return out


def _apply_kg_jaba_conversion(
    df: pl.DataFrame,
    kg_per_jaba: float | None,
) -> pl.DataFrame:
    """Si TOTAL_JABAS existe y KG_PERSONA no, deriva KG_PERSONA = jabas * factor.
    Factor por defecto = 1.0 si --kg-per-jaba no se pasa.
    """
    if "KG_PERSONA" in df.columns:
        return df
    if "TOTAL_JABAS" not in df.columns:
        return df
    factor = kg_per_jaba if kg_per_jaba is not None else 1.0
    logger.info("Derivando KG_PERSONA = TOTAL_JABAS x %.3f", factor)
    return df.with_columns(
        (pl.col("TOTAL_JABAS").cast(pl.Float64) * factor).alias("KG_PERSONA"),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build(
    input_path: Path,
    output_path: Path,
    dry_run: bool = False,
    separator: str | None = None,
    column_map: str | None = None,
    kg_per_jaba: float | None = None,
) -> pl.DataFrame:
    logger.info("Leyendo %s", input_path)
    df = _normalize(_read_input(input_path, separator=separator))

    rename_map = _parse_column_map(column_map)
    if rename_map:
        missing = [c for c in rename_map if c not in df.columns]
        if missing:
            raise ValueError(
                f"--column-map referencia columnas inexistentes: {missing}. "
                f"Disponibles: {df.columns}"
            )
        logger.info("Aplicando column-map: %s", rename_map)
        df = df.rename(rename_map)

    df = _apply_kg_jaba_conversion(df, kg_per_jaba)
    df = _derive_missing_inputs(df)
    ctx = validate_schema(df)

    df = _person_track_record(df, ctx)
    df = _supervisor_track_record(df, ctx)
    df = _person_quartile_flags(df)
    df = _add_group_continuity(df, ctx)
    df = _add_supervisor_continuity(df, ctx)
    feats = _aggregate_to_lot(df, ctx)
    logger.info("Features generadas: %d filas, %d cols", feats.height, feats.width)

    if dry_run:
        logger.info("--dry-run: no se escribe output. Preview:")
        with pl.Config(tbl_rows=10, tbl_cols=feats.width):
            print(feats.head(10))
        return feats

    output_path.parent.mkdir(parents=True, exist_ok=True)
    feats.write_parquet(output_path)
    logger.info("Escrito: %s", output_path)
    return feats


# ---------------------------------------------------------------------------
# Sample data (match real client schema, lowercase)
# ---------------------------------------------------------------------------
def make_sample(output_path: Path, n_personas: int = 200, dias: int = 540) -> Path:
    """Genera TXT tab-separado con el schema REAL del cliente (lowercase).

    Columnas emitidas (match exacto del cliente):
        fecha | formato | fundo | etapa | campo | variedad |
        idcosechador | id_supervisor | total_jabas | total_horas | temporadas

    Permite probar AUTO_ALIASES + _apply_kg_jaba_conversion + _derive_missing_inputs.
    """
    import numpy as np

    rng = np.random.default_rng(42)
    fundos = ["A9", "C5", "C6", "B2"]
    formatos = ["GRANEL", "CLAMSHELL 4.4 OZ", "CLAMSHELL 11 OZ"]
    etapas = ["PEAK", "POST-PEAK", "OFF-PEAK"]
    campos = ["F1", "F2", "F3", "F4", "F5"]
    variedad = "POP"
    supervisores = [f"SUP{i:03d}" for i in range(20)]

    base = datetime(2024, 1, 1)
    personas = [f"COS{i:04d}" for i in range(n_personas)]
    primer_dia = {p: base + timedelta(days=int(rng.integers(0, 90))) for p in personas}
    temporadas_por_persona = {
        p: int(rng.choice([1, 2, 3, 4, 5], p=[0.45, 0.25, 0.15, 0.10, 0.05]))
        for p in personas
    }
    sup_por_persona = {p: str(rng.choice(supervisores)) for p in personas}

    rows = []
    for d in range(dias):
        fecha = base + timedelta(days=d)
        n_hoy = int(rng.integers(30, 90))
        hoy = rng.choice(personas, size=n_hoy, replace=False)
        for p in hoy:
            if fecha < primer_dia[p]:
                continue
            tenure = (fecha - primer_dia[p]).days
            if tenure > 365:
                continue
            fundo = rng.choice(fundos, p=[0.5, 0.25, 0.15, 0.10])
            formato = rng.choice(formatos, p=[0.86, 0.10, 0.04])
            etapa = rng.choice(etapas, p=[0.5, 0.3, 0.2])
            campo = rng.choice(campos)
            # Rotacion ocasional de supervisor (capta MISMO_SUPERVISOR_AYER).
            sup = (
                sup_por_persona[p]
                if rng.random() > 0.10
                else str(rng.choice(supervisores))
            )
            temporadas = temporadas_por_persona[p]
            # kg/h escala con tenure (aprendizaje) y temporadas (veterania).
            kg_per_h = rng.normal(3.0 + 0.005 * tenure + 0.3 * temporadas, 1.0)
            horas = float(rng.normal(8.5, 0.6))
            jabas = max(0.5, kg_per_h * horas)
            rows.append(
                {
                    "fecha": fecha,
                    "formato": str(formato),
                    "fundo": str(fundo),
                    "etapa": str(etapa),
                    "campo": str(campo),
                    "variedad": variedad,
                    "idcosechador": p,
                    "id_supervisor": sup,
                    "total_jabas": float(jabas),
                    "total_horas": horas,
                    "temporadas": temporadas,
                }
            )

    df = pl.DataFrame(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        df.write_excel(output_path)
    elif suffix == ".parquet":
        df.write_parquet(output_path)
    elif suffix == ".csv":
        df.write_csv(output_path)
    else:
        df.write_csv(output_path, separator="\t")
    logger.info(
        "Sample generado: %s (%d filas, %d personas, %d supervisores)",
        output_path,
        df.height,
        n_personas,
        len(supervisores),
    )
    return output_path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Construye features de equipo de cosechadores (polars).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Ver docstring del modulo para schema y guia de integracion.",
    )
    p.add_argument(
        "--input",
        type=Path,
        default=Path("data/cosechadores/cosechadores.txt"),
        help="TXT/CSV/Parquet/Excel con data por cosechador. "
        "Default: %(default)s (Excel no aguanta >1M filas).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Parquet de salida (default: %(default)s).",
    )
    p.add_argument(
        "--separator",
        default=None,
        help="Separador para .txt/.csv. Autodetecta tab/coma/pipe/';' si se omite.",
    )
    p.add_argument(
        "--column-map",
        default=None,
        help="Renombra columnas tras AUTO_ALIASES. Formato: "
        "'COL_REAL=COL_ESPERADA,...'. Util cuando el nombre no calza con el alias.",
    )
    p.add_argument(
        "--kg-per-jaba",
        type=float,
        default=None,
        help="Factor para convertir TOTAL_JABAS -> KG_PERSONA. Si la data trae "
        "total_jabas y se omite, asume factor=1.0. Pregunta al cliente el peso "
        "real (puede variar por formato).",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Valida y previsualiza, no escribe."
    )
    p.add_argument(
        "--make-sample",
        action="store_true",
        help="Genera archivo de prueba en --input y termina "
        "(tab-separado si .txt, segun extension si no).",
    )
    p.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.make_sample:
        make_sample(args.input)
        logger.info(
            "Ahora corre: python -m scripts.build_cosechador_features --input %s",
            args.input,
        )
        return 0

    if not args.input.exists():
        logger.error(
            "No existe %s. Usa --make-sample para generar uno de prueba.",
            args.input,
        )
        return 1

    build(
        args.input,
        args.output,
        dry_run=args.dry_run,
        separator=args.separator,
        column_map=args.column_map,
        kg_per_jaba=args.kg_per_jaba,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Construye features de equipo de cosechadores para enriquecer el modelo KG/JR_H.

Este script vive AISLADO del pipeline actual: lee tu data por cosechador,
calcula features a nivel persona (con cuidado de leakage temporal), las
agrega al grano del modelo `(FECHA, FUNDO, FORMATO)` y las deja en un
Parquet listo para joinear con `data/training/DB-HISTORICA.xlsx`.

NO toca el pipeline ni `src/config.py`. La integracion al modelo se hace
en un segundo paso (ver seccion "Como integrar al pipeline" abajo).

Implementado con **polars** porque la data por cosechador escala a millones
de filas (1 fila por persona-dia x 12-18 meses). Polars hace rolling y
groupby vectorizado en paralelo; pandas se quedaria corto en memoria/CPU.

------------------------------------------------------------------------
SCHEMA ESPERADO del Excel/CSV de cosechadores (`--input`)
------------------------------------------------------------------------
Columnas obligatorias:
    COSECHADOR_ID         str   - ID unico de la persona (DNI, codigo, etc.)
    FECHA                 date  - fecha de cosecha
    FUNDO                 str   - mismo dominio que el modelo (A9, C5, ...)
    VARIEDAD              str   - mismo dominio que el modelo (POP, ...)
    DIA_COSECHA_PERSONA   int   - dias acumulados desde el 1er dia del año
                                  para esa persona (0..365)
    BOLSA_FLAG            0/1   - 1 si esta en la bolsa (top del año), 0 si no

Columnas opcionales (mejoran las features si estan):
    FORMATO               str   - GRANEL / CLAMSHELL ... (mismo dominio modelo)
    ETAPA                 str   - etapa del cultivo
    CAMPO                 str   - campo dentro del fundo
    GRUPO_ID              str   - ID del subgrupo dentro del lote (3-5 grupos
                                  o 1 grupo de 30). Si falta, asume 1 grupo.
    KG_PERSONA            float - kg cosechados por la persona ese dia
    HORAS_PERSONA         float - horas-efectivas trabajadas ese dia
                                  (si KG y HORAS estan presentes, se calcula
                                   KG/JR_H individual y su track record)

------------------------------------------------------------------------
SALIDA
------------------------------------------------------------------------
Parquet en `data/cosechadores/cosechador_features.parquet` con llave
`(FECHA, FUNDO, FORMATO)` mas las features agregadas. Granularidad:
    - Si `FORMATO` esta en la data -> 1 fila por (FECHA, FUNDO, FORMATO)
    - Si no -> 1 fila por (FECHA, FUNDO) y se replica a todos los formatos
              al joinear

------------------------------------------------------------------------
FEATURES GENERADAS (priorizadas por aporte esperado al modelo)
------------------------------------------------------------------------
A. Composicion del equipo:
    N_COSECHADORES, N_GRUPOS, GRUPO_SIZE_AVG, GRUPO_SIZE_STD

B. Tenure / experiencia (este año + lifetime):
    TENURE_AVG, TENURE_MEDIAN, TENURE_STD, TENURE_MIN, TENURE_MAX,
    PCT_NOVATOS  (tenure-año < NOVATO_THRESHOLD_DAYS),
    PCT_VETERANOS (tenure-año > VETERANO_THRESHOLD_DAYS),
    TENURE_LIFETIME_AVG  -- dias acumulados toda la historia (cross-year)
    PCT_PRIMER_AÑO       -- % cuyo año actual = su 1er año en la data

C. Calidad del equipo (bolsa + track record):
    PCT_BOLSA, BOLSA_DIAS_AVG,
    KG_JR_H_HIST_AVG_30D, KG_JR_H_HIST_STD_30D,    (rolling per-persona)
    PCT_TOP_HIST, PCT_BOTTOM_HIST,                 (quartiles globales)
    KG_JR_H_HIST_DOW_AVG  -- rolling 120d filtrado al MISMO dia-de-semana
                             (curva semanal a nivel cosechador)

D. Rotacion / churn:
    PCT_ROTADOS_30D            % del equipo que en 30d trabajo otro FUNDO
    FUNDOS_VISITADOS_AVG_90D
    PCT_NUEVOS_INGRESOS_14D    % cuyo 1er dia del año cae en ult. 14 dias
    PCT_DESVINCULADOS_AYER     % del equipo de ayer que no esta hoy
    DIAS_DESDE_ULTIMO_TRABAJO_AVG
    PCT_MISMA_DOTACION_AYER    % del equipo de hoy que estuvo ayer
    PCT_CAMBIO_GRUPO           % del equipo que cambio de GRUPO_ID vs ayer

E. Heterogeneidad intra-grupo y jornada (controlado por horas):
    KG_JR_H_GRUPO_CV_AVG       coef. variacion (std/mean) de KG/JR_H
                                intra-grupo, promediado al lote
    HORAS_STD_GRUPO_AVG        std de horas dentro del grupo, prom al lote
    HORAS_HIST_DOW_AVG         rolling 120d de horas en el MISMO DOW
    PCT_JORNADA_PARCIAL        % personas con HORAS<JORNADA_PARCIAL_THRESHOLD

------------------------------------------------------------------------
CLI
------------------------------------------------------------------------
    # Generar TXT de prueba (tab-separado, ~40k filas, 200 cosechadores, 18m):
    python -m scripts.build_cosechador_features --make-sample

    # Procesar tu data real (TXT tab-separado; Excel no aguanta >1M filas):
    python -m scripts.build_cosechador_features \\
        --input  data/cosechadores/cosechadores.txt \\
        --output data/cosechadores/cosechador_features.parquet

    # Validar schema sin escribir:
    python -m scripts.build_cosechador_features \\
        --input data/cosechadores/cosechadores.txt --dry-run

    # Tambien acepta CSV/Parquet/Excel; el separador del .txt se autodetecta
    # entre tab/coma/pipe/punto-y-coma. Para forzarlo: --separator '\\t'.

------------------------------------------------------------------------
COMO INTEGRAR AL PIPELINE (cuando el parquet exista)
------------------------------------------------------------------------
1) En `src/config.py`, agregar:

       COSECHADOR_FEATURES_FILE: Path = (
           DATA_DIR / "cosechadores" / "cosechador_features.parquet"
       )
       ENABLE_COSECHADOR_FEATURES: bool = _env_bool(
               "ENABLE_COSECHADOR_FEATURES", False,
       )
       # Recomendado para ablation (ver justificacion en chat de diseño):
       # Tier 1+2: alto sustento estadistico, debe entrar primero.
       # Tier 3+4: capa de signal por equipo y semanal, validar con SHAP.
       COSECHADOR_NUMERIC_FEATURES: list[str] = [
           # Tier 1 (must-have)
           "KG_JR_H_HIST_AVG_30D", "PCT_BOLSA", "TENURE_AVG",
           # Tier 2 (strong supporting)
           "PCT_NOVATOS", "PCT_MISMA_DOTACION_AYER", "PCT_ROTADOS_30D",
           # Tier 3 (cross-year + intra-grupo + cambio de grupo)
           "TENURE_LIFETIME_AVG", "PCT_PRIMER_AÑO",
           "KG_JR_H_GRUPO_CV_AVG", "HORAS_STD_GRUPO_AVG",
           "PCT_JORNADA_PARCIAL", "PCT_CAMBIO_GRUPO",
           # Tier 4 (curva semanal a nivel cosechador)
           "KG_JR_H_HIST_DOW_AVG", "HORAS_HIST_DOW_AVG",
       ]

2) En `src/step_01_load/data_loader.py`, despues del read_excel del sheet
   (el pipeline sigue usando pandas; el parquet escrito por este script
   es 100% compatible con pd.read_parquet):

       if ENABLE_COSECHADOR_FEATURES and COSECHADOR_FEATURES_FILE.exists():
           feats = pd.read_parquet(COSECHADOR_FEATURES_FILE)
           df = df.merge(feats, on=["FECHA", "FUNDO", "FORMATO"], how="left")

3) Extender `NUMERIC_FEATURES` en `config.py` con `COSECHADOR_NUMERIC_FEATURES`
   cuando el flag este prendido (o agregarlas siempre y dejar que el
   imputer maneje los NaN cuando el parquet no existe).

4) Smoke train con feature flag:
       docker compose run --rm \\
           -e ENABLE_COSECHADOR_FEATURES=1 \\
           trainer --varieties POP --tuning smoke
   Comparar MAPE_oof y gap contra baseline (LGB v3: 14.86% / 0.138).

------------------------------------------------------------------------
LEAKAGE TEMPORAL
------------------------------------------------------------------------
Todas las features de "track record" (KG_JR_H_HIST_*, BOLSA_DIAS_AVG,
PCT_ROTADOS_*) se calculan con info ESTRICTAMENTE anterior a `FECHA`.
Si una persona no tiene historial todavia (primer dia), la fila va
con NaN -> el imputer del pipeline las maneja.
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
    "DIA_COSECHA_PERSONA",
    "BOLSA_FLAG",
)
OPTIONAL_COLS: tuple[str, ...] = (
    "FORMATO",
    "ETAPA",
    "CAMPO",
    "GRUPO_ID",
    "KG_PERSONA",
    "HORAS_PERSONA",
)

# Umbrales de tenure (dias acumulados de cosecha del año).
# Tunables: ajusta tras ver la distribucion en tu data real.
NOVATO_THRESHOLD_DAYS: int = 30
VETERANO_THRESHOLD_DAYS: int = 180

# Ventanas rolling (en dias calendario, no filas).
ROLLING_HIST_DAYS: int = 30
ROTACION_WINDOW_DAYS: int = 30
FUNDOS_WINDOW_DAYS: int = 90
NUEVO_INGRESO_WINDOW_DAYS: int = 14
# DOW rolling: 120d ~ 17 ocurrencias por dia-de-semana, suficiente para mean
# estable. 30d daria ~4 ocurrencias y la estimacion seria ruidosa.
ROLLING_DOW_DAYS: int = 120
# Umbral de jornada parcial: HORAS_PERSONA < N marca dia "corto".
# 6h es el corte tipico (turno de mañana sin contar tarde).
JORNADA_PARCIAL_THRESHOLD: float = 6.0

DEFAULT_OUTPUT: Path = Path("data/cosechadores/cosechador_features.parquet")


@dataclass
class BuildContext:
    has_formato: bool
    has_grupo: bool
    has_kg_horas: bool   # KG_PERSONA + HORAS_PERSONA (ambos)
    has_horas: bool      # HORAS_PERSONA solo (subset de has_kg_horas o solo)


# ---------------------------------------------------------------------------
# Validacion de schema
# ---------------------------------------------------------------------------
def validate_schema(df: pl.DataFrame) -> BuildContext:
    """Verifica columnas requeridas y reporta cuales opcionales estan."""
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Columnas requeridas faltantes: {missing}. "
            f"Disponibles: {df.columns}"
        )
    has_formato = "FORMATO" in df.columns
    has_grupo = "GRUPO_ID" in df.columns
    has_horas = "HORAS_PERSONA" in df.columns
    has_kg_horas = ("KG_PERSONA" in df.columns) and has_horas

    n_personas = df.select(pl.col("COSECHADOR_ID").n_unique()).item()
    logger.info("Schema OK. Filas=%d, cosechadores=%d", df.height, n_personas)
    logger.info(
        "FORMATO: %s | GRUPO_ID: %s | HORAS: %s | KG/HORAS: %s",
        "si" if has_formato else "no",
        "si" if has_grupo else "no",
        "si" if has_horas else "no",
        "si" if has_kg_horas else "no",
    )
    return BuildContext(
        has_formato=has_formato,
        has_grupo=has_grupo,
        has_kg_horas=has_kg_horas,
        has_horas=has_horas,
    )


# ---------------------------------------------------------------------------
# Features a nivel persona (con anti-leakage temporal)
# ---------------------------------------------------------------------------
def _person_track_record(df: pl.DataFrame, ctx: BuildContext) -> pl.DataFrame:
    """Para cada (persona, fecha), calcula:
        - KG_JR_H_PERSONA            : kg/horas del dia (si hay datos)
        - KG_JR_H_HIST_AVG_30D       : promedio en los 30 dias ANTERIORES (closed=left)
        - KG_JR_H_HIST_STD_30D       : std en los 30 dias anteriores
        - DIAS_DESDE_ULTIMO          : dias calendario desde el ultimo dia trabajado
        - FUNDOS_VISITADOS_90D       : fundos distintos en los 90 dias previos
        - ROTADO_30D                 : 1 si en los 30 dias previos trabajo otro fundo
        - PRIMER_DIA_ANIO            : fecha del 1er dia de cosecha en el año
        - TENURE_LIFETIME            : dias unicos de cosecha acumulados de TODA
                                       la historia disponible, hasta FECHA-1
                                       (corrige el reset anual de DIA_COSECHA_PERSONA)
        - ES_PRIMER_AÑO              : 1 si el año actual es el primer año de
                                       cosecha de la persona en la data
        - KG_JR_H_HIST_DOW_AVG_120D  : rolling 120d de KG/JR_H, filtrado al
                                       MISMO dia-de-semana (capturar curva
                                       semanal a nivel cosechador)
        - HORAS_HIST_DOW_AVG_120D    : rolling 120d de HORAS, mismo DOW
                                       (balanceo por horas que pediste el user)
    """
    df = df.sort(["COSECHADOR_ID", "FECHA"])

    # KG/JR_H individual del dia (NaN si HORAS=0 para evitar div/0)
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

    # Track record numerico: rolling temporal sin leakage (closed="left"
    # excluye la fila actual). rolling_*_by + over() opera por persona.
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

    # Dias desde ultimo trabajo (shift dentro de la persona).
    df = df.with_columns(
        (pl.col("FECHA") - pl.col("FECHA").shift(1).over("COSECHADOR_ID"))
          .dt.total_days()
          .cast(pl.Float64)
          .alias("DIAS_DESDE_ULTIMO"),
    )

    # Rolling sobre FUNDO (string): no hay rolling_*_by para strings, asi
    # que usamos df.rolling().agg() con group_by. Devuelve una fila por
    # fila de input en el mismo orden -> hstack directo.
    rolled_30 = df.rolling(
        index_column="FECHA",
        period=f"{ROTACION_WINDOW_DAYS}d",
        closed="left",
        group_by="COSECHADOR_ID",
    ).agg(
        pl.col("FUNDO").unique().alias("__fundos_30d"),
    ).select("__fundos_30d")

    rolled_90 = df.rolling(
        index_column="FECHA",
        period=f"{FUNDOS_WINDOW_DAYS}d",
        closed="left",
        group_by="COSECHADOR_ID",
    ).agg(
        pl.col("FUNDO").n_unique().alias("__nfundos_90d"),
        pl.col("FUNDO").len().alias("__count_90d"),
    ).select(["__nfundos_90d", "__count_90d"])

    df = df.hstack(rolled_30).hstack(rolled_90)

    df = df.with_columns(
        # ROTADO_30D=1 si la lista past 30d contiene algo distinto del FUNDO actual.
        pl.when(pl.col("__fundos_30d").list.len() == 0)
          .then(pl.lit(0.0))
          .when(
              (pl.col("__fundos_30d").list.len() == 1)
              & pl.col("__fundos_30d").list.contains(pl.col("FUNDO"))
          )
          .then(pl.lit(0.0))
          .otherwise(pl.lit(1.0))
          .alias("ROTADO_30D"),
        # FUNDOS_VISITADOS_90D: NaN si no hay historial.
        pl.when(pl.col("__count_90d") == 0)
          .then(None)
          .otherwise(pl.col("__nfundos_90d").cast(pl.Float64))
          .alias("FUNDOS_VISITADOS_90D"),
    ).drop(["__fundos_30d", "__nfundos_90d", "__count_90d"])

    # Primer dia del año por persona (para nuevos ingresos).
    df = df.with_columns(
        pl.col("FECHA").min()
          .over(["COSECHADOR_ID", pl.col("FECHA").dt.year()])
          .alias("PRIMER_DIA_ANIO"),
    )

    # TENURE_LIFETIME: dias unicos de cosecha acumulados a lo largo de toda
    # la historia, hasta FECHA-1. Calculado sobre fechas unicas para no
    # sobrecontar si la persona aparece en multiples lotes el mismo dia.
    unique_dates = (
        df.select(["COSECHADOR_ID", "FECHA"]).unique()
          .sort(["COSECHADOR_ID", "FECHA"])
          .with_columns(
              (pl.col("FECHA").cum_count().over("COSECHADOR_ID") - 1)
                .cast(pl.Float64)
                .alias("TENURE_LIFETIME"),
          )
    )
    df = df.join(unique_dates, on=["COSECHADOR_ID", "FECHA"], how="left")

    # ES_PRIMER_AÑO: año actual == 1er año de cosecha de la persona en la data.
    # Caveat: si la data solo tiene 1 año, todos seran ES_PRIMER_AÑO=1 y la
    # feature sera constante (inutil para LightGBM). Con 2+ años discrimina.
    primer_anio = (
        df.group_by("COSECHADOR_ID")
          .agg(pl.col("FECHA").dt.year().min().alias("__primer_anio_persona"))
    )
    df = df.join(primer_anio, on="COSECHADOR_ID", how="left").with_columns(
        (pl.col("FECHA").dt.year() == pl.col("__primer_anio_persona"))
          .cast(pl.Float64)
          .alias("ES_PRIMER_AÑO"),
    ).drop("__primer_anio_persona")

    # Rolling DOW: per-persona, per-dia-de-semana, ventana 120d con
    # closed="left" (anti-leakage). Reordenar a [persona, DOW, FECHA] para
    # que rolling_mean_by + over agrupe correctamente; restaurar despues.
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

    return df


def _person_quartile_flags(df: pl.DataFrame) -> pl.DataFrame:
    """Marca top/bottom quartile usando KG_JR_H_HIST_AVG_30D del DIA ANTERIOR.

    Se calcula contra cuartiles GLOBALES de la distribucion de hist averages
    observados hasta esa fecha. Aproximacion: usamos cuartiles del año
    pasado completo. Para implementacion mas estricta, recalcular por
    rolling cuantil — al ser mas costoso, dejamos esto como heuristica
    pragmatica y suficiente para ablation.
    """
    df = df.with_columns(pl.col("FECHA").dt.year().alias("ANIO"))

    quartiles = (
        df.filter(pl.col("KG_JR_H_HIST_AVG_30D").is_not_null())
          .group_by("ANIO")
          .agg(
              pl.col("KG_JR_H_HIST_AVG_30D").quantile(0.25).alias("Q1"),
              pl.col("KG_JR_H_HIST_AVG_30D").quantile(0.75).alias("Q3"),
          )
          # mapeamos al año siguiente para evitar leakage
          .with_columns((pl.col("ANIO") + 1).alias("ANIO"))
    )
    df = df.join(quartiles, on="ANIO", how="left")
    df = df.with_columns(
        (pl.col("KG_JR_H_HIST_AVG_30D") >= pl.col("Q3")).cast(pl.Float64).alias("TOP_HIST"),
        (pl.col("KG_JR_H_HIST_AVG_30D") <= pl.col("Q1")).cast(pl.Float64).alias("BOTTOM_HIST"),
    ).drop(["Q1", "Q3", "ANIO"])
    return df


# ---------------------------------------------------------------------------
# Continuidad de grupo: cambio de GRUPO_ID respecto a ayer (mismo lote)
# ---------------------------------------------------------------------------
def _add_group_continuity(df: pl.DataFrame, ctx: BuildContext) -> pl.DataFrame:
    """Agrega CAMBIO_GRUPO a nivel persona-dia-lote.

    Para cada (persona, FECHA, FUNDO, [FORMATO]), busca su GRUPO_ID de
    AYER en el mismo lote. CAMBIO_GRUPO=1 si distinto, =0 si igual,
    NaN si no estuvo ayer (ingreso nuevo). El mean al agregar al lote
    excluye los NaN -> no contamina la señal con falsos cambios.
    """
    if not ctx.has_grupo:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("CAMBIO_GRUPO"))

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
          .otherwise(
              (pl.col("GRUPO_ID") != pl.col("__grupo_ayer")).cast(pl.Float64)
          )
          .alias("CAMBIO_GRUPO"),
    ).drop("__grupo_ayer")
    return df


# ---------------------------------------------------------------------------
# Agregacion al grano del modelo
# ---------------------------------------------------------------------------
def _aggregate_to_lot(df: pl.DataFrame, ctx: BuildContext) -> pl.DataFrame:
    """Agrupa por (FECHA, FUNDO, [FORMATO]) y emite las features de equipo."""
    keys: list[str] = ["FECHA", "FUNDO"]
    if ctx.has_formato:
        keys.append("FORMATO")

    df = df.with_columns(
        ((pl.col("FECHA") - pl.col("PRIMER_DIA_ANIO")).dt.total_days()
         <= NUEVO_INGRESO_WINDOW_DAYS)
        .cast(pl.Float64)
        .alias("__nuevo_ingreso_14d"),
    )

    aggs = [
        pl.col("COSECHADOR_ID").n_unique().alias("N_COSECHADORES"),
        pl.col("DIA_COSECHA_PERSONA").mean().alias("TENURE_AVG"),
        pl.col("DIA_COSECHA_PERSONA").median().alias("TENURE_MEDIAN"),
        pl.col("DIA_COSECHA_PERSONA").std().alias("TENURE_STD"),
        pl.col("DIA_COSECHA_PERSONA").min().alias("TENURE_MIN"),
        pl.col("DIA_COSECHA_PERSONA").max().alias("TENURE_MAX"),
        (pl.col("DIA_COSECHA_PERSONA") < NOVATO_THRESHOLD_DAYS)
            .cast(pl.Float64).mean().alias("PCT_NOVATOS"),
        (pl.col("DIA_COSECHA_PERSONA") > VETERANO_THRESHOLD_DAYS)
            .cast(pl.Float64).mean().alias("PCT_VETERANOS"),
        pl.col("BOLSA_FLAG").cast(pl.Float64).mean().alias("PCT_BOLSA"),
        pl.col("ROTADO_30D").mean().alias("PCT_ROTADOS_30D"),
        pl.col("FUNDOS_VISITADOS_90D").mean().alias("FUNDOS_VISITADOS_AVG_90D"),
        pl.col("__nuevo_ingreso_14d").mean().alias("PCT_NUEVOS_INGRESOS_14D"),
        pl.col("DIAS_DESDE_ULTIMO").mean().alias("DIAS_DESDE_ULTIMO_TRABAJO_AVG"),
        pl.col("KG_JR_H_HIST_AVG_30D").mean().alias("KG_JR_H_HIST_AVG_30D"),
        pl.col("KG_JR_H_HIST_STD_30D").mean().alias("KG_JR_H_HIST_STD_30D"),
        pl.col("TOP_HIST").mean().alias("PCT_TOP_HIST"),
        pl.col("BOTTOM_HIST").mean().alias("PCT_BOTTOM_HIST"),
        # Nuevas: experiencia cross-year y cambio de grupo.
        pl.col("TENURE_LIFETIME").mean().alias("TENURE_LIFETIME_AVG"),
        pl.col("ES_PRIMER_AÑO").mean().alias("PCT_PRIMER_AÑO"),
        # CAMBIO_GRUPO existe siempre; sera all-NaN si !has_grupo.
        pl.col("CAMBIO_GRUPO").mean().alias("PCT_CAMBIO_GRUPO"),
    ]
    if ctx.has_kg_horas:
        aggs.append(
            pl.col("KG_JR_H_HIST_DOW_AVG_120D").mean()
              .alias("KG_JR_H_HIST_DOW_AVG"),
        )
    if ctx.has_horas:
        aggs.extend([
            pl.col("HORAS_HIST_DOW_AVG_120D").mean()
              .alias("HORAS_HIST_DOW_AVG"),
            (pl.col("HORAS_PERSONA") < JORNADA_PARCIAL_THRESHOLD)
              .cast(pl.Float64).mean().alias("PCT_JORNADA_PARCIAL"),
        ])
    out = df.group_by(keys).agg(aggs)

    if ctx.has_grupo:
        gsize = (
            df.group_by(keys + ["GRUPO_ID"])
              .len()
              .rename({"len": "size"})
        )
        gstats = gsize.group_by(keys).agg(
            pl.col("GRUPO_ID").n_unique().alias("N_GRUPOS"),
            pl.col("size").mean().cast(pl.Float64).alias("GRUPO_SIZE_AVG"),
            pl.col("size").std().alias("GRUPO_SIZE_STD"),
        )
        out = out.join(gstats, on=keys, how="left")

        # Variabilidad intra-grupo de KG/JR_H y HORAS, promediada al lote.
        # CV = std/mean es scale-invariant (no penaliza grupos con mean alto).
        intra_aggs: list[pl.Expr] = []
        if ctx.has_kg_horas:
            intra_aggs.extend([
                pl.col("KG_JR_H_PERSONA").std().alias("__sd_kg"),
                pl.col("KG_JR_H_PERSONA").mean().alias("__mu_kg"),
            ])
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
    feats: pl.DataFrame, raw: pl.DataFrame, keys: list[str],
) -> pl.DataFrame:
    """Calcula PCT_MISMA_DOTACION_AYER y PCT_DESVINCULADOS_AYER por lote.

    Compara el set de COSECHADOR_ID de cada lote contra el del DIA CALENDARIO
    anterior (no la fila previa del groupby; pueden saltarse dias). Si no
    existe lote ayer, ambos quedan en NaN.
    """
    sets = (
        raw.group_by(keys)
           .agg(pl.col("COSECHADOR_ID").unique().sort().alias("set_hoy"))
    )
    # set_ayer = set_hoy del dia previo (mismo FUNDO/FORMATO, fecha-1)
    ayer = (
        sets.with_columns((pl.col("FECHA") + pl.duration(days=1)).alias("FECHA"))
            .rename({"set_hoy": "set_ayer"})
    )
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
    # set_ayer NULL (no hay lote ayer) -> NaN. set_ayer vacio -> NaN tambien
    # para PCT_DESVINCULADOS (denom 0). Se replica el comportamiento original.
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
# Entry points
# ---------------------------------------------------------------------------
def _sniff_separator(input_path: Path) -> str:
    """Detecta separador leyendo el header. Soporta tab, coma, pipe, ';'."""
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
        # pl.read_excel usa fastexcel (calamine) por default si esta instalado.
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
    """Strip de espacios en headers y cast de FECHA a Datetime."""
    renames = {c: c.strip() for c in df.columns if c != c.strip()}
    if renames:
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


def build(
    input_path: Path,
    output_path: Path,
    dry_run: bool = False,
    separator: str | None = None,
    column_map: str | None = None,
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

    ctx = validate_schema(df)

    df = _person_track_record(df, ctx)
    df = _person_quartile_flags(df)
    df = _add_group_continuity(df, ctx)
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


def make_sample(output_path: Path, n_personas: int = 200, dias: int = 540) -> Path:
    """Genera un TXT tab-separado de prueba para validar el script sin data real.

    Tab-separado porque el input real es .txt (Excel no aguanta >1M filas).
    Si pasas un path con sufijo .xlsx/.csv/.parquet, escribe en ese formato.
    Usa numpy solo para muestreo aleatorio (one-off, no afecta al pipeline).
    """
    import numpy as np

    rng = np.random.default_rng(42)
    fundos = ["A9", "C5", "C6", "B2"]
    formatos = ["GRANEL", "CLAMSHELL 4.4 OZ", "CLAMSHELL 11 OZ"]
    variedad = "POP"

    base = datetime(2024, 1, 1)
    personas = [f"COS{i:04d}" for i in range(n_personas)]
    bolsa_set = set(rng.choice(personas, size=n_personas // 4, replace=False))
    primer_dia = {
        p: base + timedelta(days=int(rng.integers(0, 90))) for p in personas
    }

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
            grupo = f"G{int(rng.integers(1, 4))}"
            kg_per_h = rng.normal(3.5 + 0.005 * tenure, 1.0)
            horas = float(rng.normal(8.5, 0.6))
            kg = max(0.5, kg_per_h * horas)
            rows.append({
                "COSECHADOR_ID": p,
                "FECHA": fecha,
                "FUNDO": str(fundo),
                "FORMATO": str(formato),
                "VARIEDAD": variedad,
                "GRUPO_ID": grupo,
                "DIA_COSECHA_PERSONA": tenure,
                "BOLSA_FLAG": int(p in bolsa_set),
                "KG_PERSONA": float(kg),
                "HORAS_PERSONA": horas,
            })

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
        # .txt (default), .tsv, o cualquier otro -> tab-separado
        df.write_csv(output_path, separator="\t")
    logger.info(
        "Sample generado: %s (%d filas, %d personas)",
        output_path, df.height, n_personas,
    )
    return output_path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Construye features de equipo de cosechadores (polars).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Ver docstring del modulo para schema y guia de integracion.",
    )
    p.add_argument("--input", type=Path,
                   default=Path("data/cosechadores/cosechadores.txt"),
                   help="TXT/CSV/Parquet/Excel con data por cosechador (ver schema). "
                        "Default: %(default)s (Excel no aguanta >1M filas).")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                   help="Parquet de salida (default: %(default)s).")
    p.add_argument("--separator", default=None,
                   help="Separador para .txt/.csv. Si se omite, se autodetecta "
                        "entre tab/coma/pipe/';'. Usa $'\\t' en bash o `t para PowerShell.")
    p.add_argument("--column-map", default=None,
                   help="Renombra columnas reales -> esperadas. Formato: "
                        "'COL_REAL=COL_ESPERADA,OTRA=OTRA_ESPERADA'. Ejemplo: "
                        "'DNI=COSECHADOR_ID,FECHA_COSECHA=FECHA'. Util cuando "
                        "el archivo del cliente trae nombres distintos a los "
                        "documentados en el schema.")
    p.add_argument("--dry-run", action="store_true",
                   help="Valida y previsualiza, no escribe.")
    p.add_argument("--make-sample", action="store_true",
                   help="Genera archivo de prueba en --input y termina "
                        "(tab-separado si .txt, segun extension si no).")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
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
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

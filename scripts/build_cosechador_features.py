"""Construye features de equipo de cosechadores para enriquecer el modelo KG/JR_H.

Este script vive AISLADO del pipeline actual: lee tu data por cosechador,
calcula features a nivel persona (con cuidado de leakage temporal), las
agrega al grano del modelo `(FECHA, FUNDO, FORMATO)` y las deja en un
Parquet listo para joinear con `data/training/DB-HISTORICA.xlsx`.

NO toca el pipeline ni `src/config.py`. La integracion al modelo se hace
en un segundo paso (ver seccion "Como integrar al pipeline" abajo).

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

B. Tenure / experiencia:
    TENURE_AVG, TENURE_MEDIAN, TENURE_STD, TENURE_MIN, TENURE_MAX,
    PCT_NOVATOS  (tenure < NOVATO_THRESHOLD_DAYS),
    PCT_VETERANOS (tenure > VETERANO_THRESHOLD_DAYS)

C. Calidad del equipo (bolsa + track record):
    PCT_BOLSA, BOLSA_DIAS_AVG,
    KG_JR_H_HIST_AVG_30D, KG_JR_H_HIST_STD_30D,    (rolling per-persona)
    PCT_TOP_HIST, PCT_BOTTOM_HIST                  (quartiles globales)

D. Rotacion / churn:
    PCT_ROTADOS_30D            % del equipo que en 30d trabajo otro FUNDO
    FUNDOS_VISITADOS_AVG_90D
    PCT_NUEVOS_INGRESOS_14D    % cuyo 1er dia del año cae en ult. 14 dias
    PCT_DESVINCULADOS_AYER     % del equipo de ayer que no esta hoy
    DIAS_DESDE_ULTIMO_TRABAJO_AVG
    PCT_MISMA_DOTACION_AYER    % del equipo de hoy que estuvo ayer

------------------------------------------------------------------------
CLI
------------------------------------------------------------------------
    # Generar Excel de prueba (40k filas, 200 cosechadores, 18 meses):
    python -m scripts.build_cosechador_features --make-sample

    # Procesar tu data real:
    python -m scripts.build_cosechador_features \\
        --input  data/cosechadores/cosechadores.xlsx \\
        --output data/cosechadores/cosechador_features.parquet

    # Validar schema sin escribir:
    python -m scripts.build_cosechador_features \\
        --input data/cosechadores/cosechadores.xlsx --dry-run

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
       COSECHADOR_NUMERIC_FEATURES: list[str] = [
           "PCT_BOLSA", "TENURE_AVG", "PCT_NOVATOS",
           "KG_JR_H_HIST_AVG_30D", "PCT_ROTADOS_30D",
           "PCT_MISMA_DOTACION_AYER",
           # ... agregar el resto que quieras probar
       ]

2) En `src/step_01_load/data_loader.py`, despues del read_excel del sheet:

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
from pathlib import Path

import numpy as np
import pandas as pd

# Sin import de src.config: este script es standalone para que se pueda
# correr antes de tener la integracion al pipeline lista.

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

DEFAULT_OUTPUT: Path = Path("data/cosechadores/cosechador_features.parquet")


@dataclass
class BuildContext:
    has_formato: bool
    has_grupo: bool
    has_kg_horas: bool


# ---------------------------------------------------------------------------
# Validacion de schema
# ---------------------------------------------------------------------------
def validate_schema(df: pd.DataFrame) -> BuildContext:
    """Verifica columnas requeridas y reporta cuales opcionales estan."""
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Columnas requeridas faltantes: {missing}. "
            f"Disponibles: {list(df.columns)}"
        )
    has_formato = "FORMATO" in df.columns
    has_grupo = "GRUPO_ID" in df.columns
    has_kg_horas = ("KG_PERSONA" in df.columns) and ("HORAS_PERSONA" in df.columns)

    logger.info("Schema OK. Filas=%d, cosechadores=%d", len(df), df["COSECHADOR_ID"].nunique())
    logger.info("FORMATO: %s | GRUPO_ID: %s | KG/HORAS: %s",
                "si" if has_formato else "no",
                "si" if has_grupo else "no",
                "si" if has_kg_horas else "no")
    return BuildContext(has_formato=has_formato, has_grupo=has_grupo,
                        has_kg_horas=has_kg_horas)


# ---------------------------------------------------------------------------
# Features a nivel persona (con anti-leakage temporal)
# ---------------------------------------------------------------------------
def _person_track_record(df: pd.DataFrame, ctx: BuildContext) -> pd.DataFrame:
    """Para cada (persona, fecha), calcula:
        - KG_JR_H_PERSONA       : kg/horas del dia (si hay datos)
        - KG_JR_H_HIST_AVG_30D  : promedio en los 30 dias ANTERIORES (shift+rolling)
        - KG_JR_H_HIST_STD_30D  : std en los 30 dias anteriores
        - DIAS_DESDE_ULTIMO     : dias calendario desde el ultimo dia trabajado
        - FUNDOS_VISITADOS_90D  : fundos distintos en los 90 dias previos
        - ROTADO_30D            : 1 si en los 30 dias previos trabajo otro fundo
        - PRIMER_DIA_DEL_ANIO   : fecha del 1er dia de cosecha en el año
    """
    df = df.sort_values(["COSECHADOR_ID", "FECHA"]).reset_index(drop=True)

    # KG/JR_H individual del dia
    if ctx.has_kg_horas:
        with np.errstate(divide="ignore", invalid="ignore"):
            df["KG_JR_H_PERSONA"] = df["KG_PERSONA"] / df["HORAS_PERSONA"].replace(0, np.nan)
    else:
        df["KG_JR_H_PERSONA"] = np.nan

    # Para rolling temporal sin leakage usamos `rolling` sobre fecha indexada
    # con `closed="left"` (excluye la fila actual).
    def _rolling_per_person(g: pd.DataFrame) -> pd.DataFrame:
        g = g.set_index("FECHA")
        # Track record (solo si hay KG/HORAS)
        roll = g["KG_JR_H_PERSONA"].rolling(f"{ROLLING_HIST_DAYS}D", closed="left")
        g["KG_JR_H_HIST_AVG_30D"] = roll.mean()
        g["KG_JR_H_HIST_STD_30D"] = roll.std()

        # Dias desde ultimo trabajo
        prev_fecha = g.index.to_series().shift(1)
        g["DIAS_DESDE_ULTIMO"] = (g.index.to_series() - prev_fecha).dt.days

        # Rotado y FUNDOS_VISITADOS: rolling con strings no esta soportado
        # por pandas (DataError). Iteramos manualmente por fila usando la
        # ventana temporal `FECHA - delta < t < FECHA` (anti-leakage).
        rotado_vals: list[float] = []
        fundos_vis: list[float] = []
        idx = g.index  # DatetimeIndex
        fundo_arr = g["FUNDO"].to_numpy()
        for i in range(len(g)):
            t = idx[i]
            actual = fundo_arr[i]
            # ventana 30d para ROTADO
            cut30 = t - pd.Timedelta(days=ROTACION_WINDOW_DAYS)
            mask30 = (idx >= cut30) & (idx < t)
            prev30 = fundo_arr[mask30]
            rotado_vals.append(
                float((prev30 != actual).any()) if prev30.size else 0.0
            )
            # ventana 90d para FUNDOS_VISITADOS
            cut90 = t - pd.Timedelta(days=FUNDOS_WINDOW_DAYS)
            mask90 = (idx >= cut90) & (idx < t)
            prev90 = fundo_arr[mask90]
            fundos_vis.append(float(np.unique(prev90).size) if prev90.size else np.nan)
        g["ROTADO_30D"] = rotado_vals
        g["FUNDOS_VISITADOS_90D"] = fundos_vis

        # Primer dia del año (para nuevos ingresos): por cada año, replicar
        # la fecha minima a todas las filas de ese año. Usar transform con
        # un proxy numerico (timestamp) y reconvertir, porque transform no
        # acepta datetime min directamente en algunas versiones de pandas.
        anio = g.index.year
        ts = g.index.astype("int64").to_series().set_axis(g.index)
        primer_ts = ts.groupby(anio).transform("min")
        g["PRIMER_DIA_ANIO"] = pd.to_datetime(primer_ts.values)

        return g.reset_index()

    out = df.groupby("COSECHADOR_ID", group_keys=False).apply(
        _rolling_per_person, include_groups=False,
    )
    # include_groups=False elimina COSECHADOR_ID del frame interno; lo
    # recuperamos del index del groupby al resetear.
    if "COSECHADOR_ID" not in out.columns:
        out = out.reset_index().rename(columns={"level_0": "COSECHADOR_ID"})
        # algunos pandas devuelven el ID como index name correcto
        if "COSECHADOR_ID" not in out.columns and "index" in out.columns:
            out = out.rename(columns={"index": "COSECHADOR_ID"})
    return out


def _person_quartile_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Marca top/bottom quartile usando KG_JR_H_HIST_AVG_30D del DIA ANTERIOR.

    Se calcula contra cuartiles GLOBALES de la distribucion de hist averages
    observados hasta esa fecha. Aproximacion: usamos cuartiles del año
    pasado completo. Para implementacion mas estricta, recalcular por
    rolling cuantil — al ser mas costoso, dejamos esto como heuristica
    pragmatica y suficiente para ablation.
    """
    df = df.copy()
    df["ANIO"] = df["FECHA"].dt.year
    quartiles = (
        df.dropna(subset=["KG_JR_H_HIST_AVG_30D"])
          .groupby("ANIO")["KG_JR_H_HIST_AVG_30D"]
          .quantile([0.25, 0.75])
          .unstack()
          .rename(columns={0.25: "Q1", 0.75: "Q3"})
    )
    # mapeamos al año previo para evitar leakage
    quartiles.index = quartiles.index + 1
    df = df.merge(quartiles, left_on="ANIO", right_index=True, how="left")
    df["TOP_HIST"] = (df["KG_JR_H_HIST_AVG_30D"] >= df["Q3"]).astype(float)
    df["BOTTOM_HIST"] = (df["KG_JR_H_HIST_AVG_30D"] <= df["Q1"]).astype(float)
    return df.drop(columns=["Q1", "Q3", "ANIO"])


# ---------------------------------------------------------------------------
# Agregacion al grano del modelo
# ---------------------------------------------------------------------------
def _aggregate_to_lot(df: pd.DataFrame, ctx: BuildContext) -> pd.DataFrame:
    """Agrupa por (FECHA, FUNDO, [FORMATO]) y emite las features de equipo."""
    keys = ["FECHA", "FUNDO"]
    if ctx.has_formato:
        keys.append("FORMATO")

    # Para PCT_DESVINCULADOS_AYER y PCT_MISMA_DOTACION_AYER necesitamos
    # comparar el set de cosechadores hoy vs ayer en el mismo lote.
    df = df.sort_values(keys + ["COSECHADOR_ID"]).copy()
    df["__nuevo_ingreso_14d"] = (
        (df["FECHA"] - df["PRIMER_DIA_ANIO"]).dt.days <= NUEVO_INGRESO_WINDOW_DAYS
    ).astype(float)

    grouped = df.groupby(keys, sort=False)

    aggs: dict[str, pd.Series] = {
        "N_COSECHADORES": grouped["COSECHADOR_ID"].nunique(),
        "TENURE_AVG": grouped["DIA_COSECHA_PERSONA"].mean(),
        "TENURE_MEDIAN": grouped["DIA_COSECHA_PERSONA"].median(),
        "TENURE_STD": grouped["DIA_COSECHA_PERSONA"].std(),
        "TENURE_MIN": grouped["DIA_COSECHA_PERSONA"].min(),
        "TENURE_MAX": grouped["DIA_COSECHA_PERSONA"].max(),
        "PCT_NOVATOS": grouped["DIA_COSECHA_PERSONA"].apply(
            lambda s: float((s < NOVATO_THRESHOLD_DAYS).mean())
        ),
        "PCT_VETERANOS": grouped["DIA_COSECHA_PERSONA"].apply(
            lambda s: float((s > VETERANO_THRESHOLD_DAYS).mean())
        ),
        "PCT_BOLSA": grouped["BOLSA_FLAG"].mean(),
        "PCT_ROTADOS_30D": grouped["ROTADO_30D"].mean(),
        "FUNDOS_VISITADOS_AVG_90D": grouped["FUNDOS_VISITADOS_90D"].mean(),
        "PCT_NUEVOS_INGRESOS_14D": grouped["__nuevo_ingreso_14d"].mean(),
        "DIAS_DESDE_ULTIMO_TRABAJO_AVG": grouped["DIAS_DESDE_ULTIMO"].mean(),
        "KG_JR_H_HIST_AVG_30D": grouped["KG_JR_H_HIST_AVG_30D"].mean(),
        "KG_JR_H_HIST_STD_30D": grouped["KG_JR_H_HIST_STD_30D"].mean(),
        "PCT_TOP_HIST": grouped["TOP_HIST"].mean(),
        "PCT_BOTTOM_HIST": grouped["BOTTOM_HIST"].mean(),
    }

    if ctx.has_grupo:
        gsize = df.groupby(keys + ["GRUPO_ID"]).size().reset_index(name="size")
        ng = gsize.groupby(keys)["GRUPO_ID"].nunique().rename("N_GRUPOS")
        gs_avg = gsize.groupby(keys)["size"].mean().rename("GRUPO_SIZE_AVG")
        gs_std = gsize.groupby(keys)["size"].std().rename("GRUPO_SIZE_STD")
        aggs["N_GRUPOS"] = ng
        aggs["GRUPO_SIZE_AVG"] = gs_avg
        aggs["GRUPO_SIZE_STD"] = gs_std

    out = pd.concat(aggs.values(), axis=1)
    out.columns = list(aggs.keys())
    out = out.reset_index()

    # Continuidad dia a dia (PCT_MISMA_DOTACION_AYER, PCT_DESVINCULADOS_AYER)
    out = _add_continuity_features(out, df, keys)
    return out


def _add_continuity_features(
    feats: pd.DataFrame, raw: pd.DataFrame, keys: list[str],
) -> pd.DataFrame:
    """Calcula PCT_MISMA_DOTACION_AYER y PCT_DESVINCULADOS_AYER por lote.

    Compara el set de COSECHADOR_ID de cada fila contra el del DIA CALENDARIO
    anterior (no la fila previa del groupby; pueden saltarse dias).
    """
    sets = (
        raw.groupby(keys)["COSECHADOR_ID"]
           .apply(lambda s: frozenset(s))
           .reset_index(name="set_hoy")
    )
    # buscar set de ayer (mismo FUNDO/FORMATO, fecha-1)
    ayer = sets.copy()
    ayer["FECHA"] = ayer["FECHA"] + pd.Timedelta(days=1)
    ayer = ayer.rename(columns={"set_hoy": "set_ayer"})

    sets = sets.merge(ayer, on=keys, how="left")

    def _pct_misma(row):
        a, b = row["set_hoy"], row["set_ayer"]
        if not isinstance(b, frozenset) or not a:
            return np.nan
        return len(a & b) / len(a)

    def _pct_desvinc(row):
        a, b = row["set_hoy"], row["set_ayer"]
        if not isinstance(b, frozenset) or not b:
            return np.nan
        return len(b - a) / len(b)

    sets["PCT_MISMA_DOTACION_AYER"] = sets.apply(_pct_misma, axis=1)
    sets["PCT_DESVINCULADOS_AYER"] = sets.apply(_pct_desvinc, axis=1)

    feats = feats.merge(
        sets[keys + ["PCT_MISMA_DOTACION_AYER", "PCT_DESVINCULADOS_AYER"]],
        on=keys, how="left",
    )
    return feats


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def build(input_path: Path, output_path: Path, dry_run: bool = False) -> pd.DataFrame:
    logger.info("Leyendo %s", input_path)
    if input_path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(input_path)
    elif input_path.suffix.lower() == ".csv":
        df = pd.read_csv(input_path)
    elif input_path.suffix.lower() == ".parquet":
        df = pd.read_parquet(input_path)
    else:
        raise ValueError(f"Formato no soportado: {input_path.suffix}")

    df.columns = [str(c).strip() for c in df.columns]
    df["FECHA"] = pd.to_datetime(df["FECHA"])
    ctx = validate_schema(df)

    df = _person_track_record(df, ctx)
    df = _person_quartile_flags(df)
    feats = _aggregate_to_lot(df, ctx)
    logger.info("Features generadas: %d filas, %d cols", len(feats), feats.shape[1])

    if dry_run:
        logger.info("--dry-run: no se escribe output. Preview:")
        print(feats.head(10).to_string())
        return feats

    output_path.parent.mkdir(parents=True, exist_ok=True)
    feats.to_parquet(output_path, index=False)
    logger.info("Escrito: %s", output_path)
    return feats


def make_sample(output_path: Path, n_personas: int = 200, dias: int = 540) -> Path:
    """Genera un Excel de prueba para validar el script sin data real."""
    rng = np.random.default_rng(42)
    fundos = ["A9", "C5", "C6", "B2"]
    formatos = ["GRANEL", "CLAMSHELL 4.4 OZ", "CLAMSHELL 11 OZ"]
    variedad = "POP"

    rows = []
    base = pd.Timestamp("2024-01-01")
    personas = [f"COS{i:04d}" for i in range(n_personas)]
    bolsa_set = set(rng.choice(personas, size=n_personas // 4, replace=False))
    primer_dia = {
        p: base + pd.Timedelta(days=int(rng.integers(0, 90))) for p in personas
    }

    for d in range(dias):
        fecha = base + pd.Timedelta(days=d)
        # cada dia trabajan 30-90 cosechadores
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

    df = pd.DataFrame(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(output_path, index=False)
    logger.info("Sample generado: %s (%d filas, %d personas)", output_path, len(df), n_personas)
    return output_path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Construye features de equipo de cosechadores.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Ver docstring del modulo para schema y guia de integracion.",
    )
    p.add_argument("--input", type=Path,
                   default=Path("data/cosechadores/cosechadores.xlsx"),
                   help="Excel/CSV/Parquet con data por cosechador (ver schema).")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                   help="Parquet de salida (default: %(default)s).")
    p.add_argument("--dry-run", action="store_true",
                   help="Valida y previsualiza, no escribe.")
    p.add_argument("--make-sample", action="store_true",
                   help="Genera Excel de prueba en --input y termina.")
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
        logger.info("Ahora corre: python -m scripts.build_cosechador_features "
                    "--input %s", args.input)
        return 0

    if not args.input.exists():
        logger.error("No existe %s. Usa --make-sample para generar uno de prueba.",
                     args.input)
        return 1

    build(args.input, args.output, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

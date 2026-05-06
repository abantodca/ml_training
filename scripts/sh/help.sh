#!/usr/bin/env bash
# Indice navegable de tasks. Lo imprime `task` (sin args) via la tarea `default`.
# Mantener sincronizado con Taskfile.yml.

cat <<'EOF'

=============================================================================
 ml_training — tasks (LOCAL-ONLY)
=============================================================================

ENTORNO:
  Local (Windows / Linux). MLflow sqlite en mlruns/mlflow.db (Model Registry
  habilitado). Sin AWS.

--- SETUP & DATA -----------------------------------------------------------
   task setup                    Instala deps Python (pip install -r requirements.txt)
   task data:split               Genera data/training/DB-HISTORICA.xlsx
                                   (split por variedad desde el acumulado)

--- TRAINING ---------------------------------------------------------------
   task train VARIETIES=POP                       Entrena 1 variedad (TUNING=prod default)
   task train VARIETIES=POP TUNING=dev            Baseline rapido (~20 min)
   task train VARIETIES=POP TUNING=prod           Produccion (~1.5h, default)
   task train VARIETIES=POP,VENTURA               Varias variedades

   Cada variedad es independiente: si una falla, las demas continuan.
   Pipeline:  XGB vs LGB  ->  champion por variedad
              (lex-order: gap -> MAPE -> tiempo). MODEL=auto (default)
              entrena ambos y el lex-order resuelve. Losers se eliminan
              automaticamente de MLflow Experiments (en mlruns/.trash/,
              recuperables).
   Si DB-HISTORICA.xlsx no existe, se genera automaticamente.

--- MLFLOW (UI local con Model Registry) -----------------------------------
   task mlflow:ui                Abre la UI en http://localhost:5000
                                   (backend sqlite -> tab "Models" funcional,
                                    versionado real del campeon registrado)

--- LOGS / AUDITORIA --------------------------------------------------------
   task logs:local                              tail al log del orquestador
   task logs:variety VARIETY=POP                tail al log de UNA variedad
   task audit:compare -- --variety POP --last 5 Comparativa entre runs (KG/JR)

--- CLEANUP ---------------------------------------------------------------
   task clean:outputs                  Borra artifacts/, logs/, reports/, __pycache__/
   task clean:artifacts KEEP=10        Conserva los ultimos 10 runs por (variety, model)

Pipeline en src/: step_01..06 (load > clean > features > train > evaluate > track)

-----------------------------------------------------------------------------
 Tip:  task --list   muestra el listado plano alfabetico (todas las tasks)
=============================================================================

EOF

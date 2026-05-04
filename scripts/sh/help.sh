#!/usr/bin/env bash
# Indice navegable de tasks. Lo imprime `task` (sin args) via la tarea `default`.
# Mantener sincronizado con Taskfile.yml.

cat <<'EOF'

=============================================================================
 ml_training — tasks (LOCAL-ONLY)
=============================================================================

ENTORNO:
  Local (Windows / Linux). MLflow file:// en ./mlruns. Sin AWS.

--- SETUP & DATA -----------------------------------------------------------
   task setup                    Instala deps Python (pip install -r requirements.txt)
   task data:split               Genera data/training/DB-HISTORICA.xlsx
                                   (split por variedad desde el acumulado)

--- TRAINING ---------------------------------------------------------------
   task train VARIETIES=POP               Entrena 1 variedad
   task train VARIETIES=POP,VENTURA       Entrena varias variedades
   task train VARIETIES=all               Entrena todas las variedades

   Cada variedad es independiente: si una falla, las demas continuan.
   Pipeline siempre:  XGB vs LGB  →  campeon  →  GAM ajusta errores del campeon
   GAM es el default. Auto-fallback incluido: si GAM no mejora, se desactiva
   solo y el modelo entrega la prediccion del base puro sin intervencion.
   Si DB-HISTORICA.xlsx no existe, se genera automaticamente.

   Para desactivar GAM (solo base XGB/LGB):
   task train VARIETIES=POP STACKING=none

--- MLFLOW (UI local) -------------------------------------------------------
   task mlflow:ui                Abre la UI en http://localhost:5000

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

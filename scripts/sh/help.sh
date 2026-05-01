#!/usr/bin/env bash
# Indice navegable de tasks. Lo imprime `task` (sin args) via la tarea `default`.
# Mantener sincronizado con el header de Taskfile.yml.

cat <<'EOF'

=============================================================================
 ml_training — tasks
=============================================================================

DOS ENTORNOS, AISLADOS:
  LOCAL  -> corre en Windows. MLflow file:// en ./mlruns. No toca AWS.
  AWS    -> corre en EC2. MLflow remoto + S3. Codigo viaja via S3.

VARIABLES (sobrescribibles):
  VARIETIES=POP|POP,VENTURA|all   PARALLEL=N   (variedades a entrenar)
  TUNING=smoke|dev|prod           (presupuesto Optuna; NO es entorno)

  Avanzadas (raras veces se tocan):
    MODEL=auto|xgb|lgb|xgb,lgb|all   (default 'auto' = entrena XGB y LGB
                                      con Optuna independiente y elige
                                      campeon por variedad. 'xgb' o 'lgb'
                                      fuerza un solo backend, ~50% mas rapido)
    STAGE=Staging|Production         (registra el campeon en MLflow Registry)

--- LOCAL (Windows) ---------------------------------------------------------
   task setup                        Instala deps Python
   task data:split                   Regenera data/training/DB-HISTORICA.xlsx
                                       (split por variedad desde el acumulado)
   task smoke                        Pipeline smoke (~1 min)
   task train:local VARIETIES=POP    Entrena (acepta CSV o 'all')
                                       Si DB-HISTORICA.xlsx no existe, hace split auto

--- AWS (EC2 + MLflow remoto) -----------------------------------------------
   task run VARIETIES=all PARALLEL=4 TODO-EN-UNO (power+deploy+train)

   Manual paso a paso:
   task power:on                                 [1/4] Prende EC2 (training+mlflow)
   task deploy:upload-code                       [2/4] tar.gz src/ -> S3 (EC2 lo baja)
   task deploy:upload-data                       [3/4] sync data/*.xlsx -> S3
   task train:remote VARIETIES=POP               [4/4] entrena (CSV o 'all')
   task power:off                                Apaga EC2 (cuida costo)
   task power:status                             Estado EC2 (running/stopped)
   task ssh:training / ssh:mlflow                SSH directo

ARTIFACTS + MLFLOW (solo entorno AWS)
   task mlflow:open                              UI (modelos, reportes, summaries)
   task mlflow:status                            Health check MLflow
   task audit:compare -- --variety POP --last 5  Comparativa entre runs (KG/JR)

LOGS
   task logs:{local,remote,mlflow,postgres}
   task logs:variety VARIETY=POP
   task logs:cloud-init-{training,mlflow}

--- INFRA (Terraform) -------------------------------------------------------
   task infra:init / infra:fmt / infra:validate
   task infra:plan                               Genera tfplan
   task infra:apply                              Aplica + regenera .env.infra
   task infra:up                                 plan + apply en uno
   task infra:output                             URLs, IPs, env_block
   task infra:env-export                         Regenera .env.infra manual
   task infra:destroy                            CUIDADO: borra todo (con prompt)

Pipeline en src/: step_01..06 (load > clean > features > train > evaluate > track)

-----------------------------------------------------------------------------
 Tip:  task --list   muestra el listado plano alfabetico (todas las tasks)
=============================================================================

EOF

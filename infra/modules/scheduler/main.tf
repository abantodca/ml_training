data "aws_region"          "current" {}
data "aws_caller_identity" "current" {}

# Convertir horario local (PET) a UTC para los cron expressions.
# Perú no usa DST, así que el offset es estable; otras TZs requerirían
# lógica condicional o dos rules (verano/invierno).
locals {
  start_hour_utc = (var.work_start_hour_local - var.tz_offset_hours) % 24
  end_hour_utc   = (var.work_end_hour_local - var.tz_offset_hours) % 24
}

# Lambda que apaga o enciende MLflow+RDS según el parámetro `action`
# del evento de EventBridge.
data "archive_file" "scheduler" {
  type        = "zip"
  source_dir  = "${var.lambdas_src_dir}/scheduler"
  output_path = "${path.module}/.build/scheduler.zip"
}

resource "aws_iam_role" "scheduler" {
  name = "${var.project}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "scheduler_basic" {
  role       = aws_iam_role.scheduler.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "scheduler_ops" {
  name = "ops"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = [
          "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:service/${var.ecs_cluster_name}/${var.ecs_service_name_mlflow}",
          "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:service/${var.ecs_cluster_name}/${var.ecs_service_name_reports}",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["rds:StartDBInstance", "rds:StopDBInstance", "rds:DescribeDBInstances"]
        Resource = "arn:aws:rds:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:db:${var.rds_instance_id}"
      },
      {
        # Necesario para chequear jobs Batch activos antes del shutdown
        # (regla anti-conflicto — ver decisión 3 de diseño).
        Effect   = "Allow"
        Action   = ["batch:ListJobs"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "scheduler" {
  name              = "/aws/lambda/${var.project}-scheduler"
  retention_in_days = 30
}

resource "aws_lambda_function" "scheduler" {
  function_name    = "${var.project}-mlflow-scheduler"
  role             = aws_iam_role.scheduler.arn
  filename         = data.archive_file.scheduler.output_path
  source_code_hash = data.archive_file.scheduler.output_base64sha256
  runtime          = "python3.13"
  handler          = "scheduler.lambda_handler"
  timeout          = 60
  memory_size      = 128

  environment {
    variables = {
      ECS_CLUSTER         = var.ecs_cluster_name
      ECS_SERVICE_MLFLOW  = var.ecs_service_name_mlflow
      ECS_SERVICE_REPORTS = var.ecs_service_name_reports
      RDS_INSTANCE_ID     = var.rds_instance_id
      # Lista separada por comas; el shutdown chequea jobs activos antes de apagar.
      BATCH_QUEUES        = "${var.batch_queue_spot_name},${var.batch_queue_ondemand_name}"
    }
  }

  depends_on = [aws_cloudwatch_log_group.scheduler]
}

# ─── Cron startup: L-V a las work_start_hour_local PET ───────────────────────

resource "aws_cloudwatch_event_rule" "startup" {
  name                = "${var.project}-mlflow-startup"
  description         = "Enciende MLflow+RDS L-V ${var.work_start_hour_local}:00 ${var.tz_offset_hours == -5 ? "PET" : "local"}"
  schedule_expression = "cron(0 ${local.start_hour_utc} ? * ${var.workdays_cron} *)"
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "startup" {
  rule  = aws_cloudwatch_event_rule.startup.name
  arn   = aws_lambda_function.scheduler.arn
  input = jsonencode({ action = "start" })
}

resource "aws_lambda_permission" "startup" {
  statement_id  = "eb-startup"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.startup.arn
}

# ─── Cron shutdown: L-V a las work_end_hour_local PET ────────────────────────

resource "aws_cloudwatch_event_rule" "shutdown" {
  name                = "${var.project}-mlflow-shutdown"
  description         = "Apaga MLflow+RDS L-V ${var.work_end_hour_local}:00 ${var.tz_offset_hours == -5 ? "PET" : "local"}"
  schedule_expression = "cron(0 ${local.end_hour_utc} ? * ${var.workdays_cron} *)"
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "shutdown" {
  rule  = aws_cloudwatch_event_rule.shutdown.name
  arn   = aws_lambda_function.scheduler.arn
  input = jsonencode({ action = "stop" })
}

resource "aws_lambda_permission" "shutdown" {
  statement_id  = "eb-shutdown"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.shutdown.arn
}
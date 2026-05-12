resource "aws_sns_topic" "alerts" {
  name = "${var.project}-alerts"
}

# La suscripción email requiere confirmación manual desde el inbox.
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ─── Alarma 1: cualquier job FAILED en la última hora ────────────────────────

resource "aws_cloudwatch_metric_alarm" "job_failures" {
  alarm_name          = "${var.project}-job-failures"
  namespace           = "AWS/Batch"
  metric_name         = "FailedJobCount"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { JobQueue = var.batch_job_queue_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# ─── Alarma 2: degradación del modelo (MAPE > umbral) ────────────────────────
# Se alimenta del custom metric que el trainer emite con EMIT_CW_METRICS=1.

resource "aws_cloudwatch_metric_alarm" "mape_pop" {
  alarm_name          = "${var.project}-mape-pop-too-high"
  namespace           = "MLTraining"
  metric_name         = "BusinessMAPE"
  statistic           = "Average"
  period              = 86400
  evaluation_periods  = 1
  threshold           = var.mape_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { Variety = "POP" }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# ─── Alarma 3: MLflow Fargate unhealthy 5+ minutos ───────────────────────────

locals {
  alb_name = element(split("/", var.alb_arn), length(split("/", var.alb_arn)) - 2)
  alb_id   = element(split("/", var.alb_arn), length(split("/", var.alb_arn)) - 1)
  tg_name  = element(split("/", var.tg_arn), length(split("/", var.tg_arn)) - 2)
  tg_id    = element(split("/", var.tg_arn), length(split("/", var.tg_arn)) - 1)
}

resource "aws_cloudwatch_metric_alarm" "mlflow_unhealthy" {
  alarm_name          = "${var.project}-mlflow-unhealthy"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions = {
    TargetGroup  = "targetgroup/${local.tg_name}/${local.tg_id}"
    LoadBalancer = "app/${local.alb_name}/${local.alb_id}"
  }
  alarm_actions = [aws_sns_topic.alerts.arn]
}
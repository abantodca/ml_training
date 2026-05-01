# Modulo: Lambda que apaga/prende las EC2 del proyecto via tag.
# Postgres y MLflow viven DENTRO de la EC2 del MLflow server; cuando la
# instancia para, todo para. Cuando arranca, systemd levanta postgres y
# mlflow.service en orden (Requires=postgresql.service).

# Empaquetado: zip con el handler.py
data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/code"
  output_path = "${path.module}/build/handler.zip"
}

# IAM
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name_prefix}-power-lambda"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# Permisos:
#   - DescribeInstances NO soporta resource-level perms -> abierto, pero el
#     handler filtra por tag Project antes de devolver IDs.
#   - StartInstances / StopInstances restringidos por condition al tag
#     Project = <project_name>: la lambda no puede tocar instancias ajenas.
data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "DescribeInstancesAll"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }

  statement {
    sid       = "StartStopOnlyTagged"
    actions   = ["ec2:StartInstances", "ec2:StopInstances"]
    resources = ["arn:aws:ec2:*:*:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Project"
      values   = [var.project_name]
    }
  }

  statement {
    sid = "CloudWatchLogs"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

# Lambda
resource "aws_lambda_function" "power" {
  function_name    = "${var.name_prefix}-power-manager"
  role             = aws_iam_role.lambda.arn
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.11"
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      PROJECT_NAME = var.project_name
    }
  }

  tags = { Name = "${var.name_prefix}-power-manager" }
}

# Log group con retencion (mantiene CloudWatch barato)
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${aws_lambda_function.power.function_name}"
  retention_in_days = 7
}

# ---------- Schedule opcional (off-hours stop / morning start) ----------
# Comentado por defecto. Para activar: set var.enable_schedule = true en
# main.tf y descomenta el modulo. CRON en UTC.
#
# resource "aws_cloudwatch_event_rule" "stop_nightly" {
#   count               = var.enable_schedule ? 1 : 0
#   name                = "${var.name_prefix}-stop-nightly"
#   schedule_expression = "cron(0 1 * * ? *)" # 01:00 UTC = 20:00 PE
# }
# resource "aws_cloudwatch_event_target" "stop_nightly" {
#   count = var.enable_schedule ? 1 : 0
#   rule  = aws_cloudwatch_event_rule.stop_nightly[0].name
#   arn   = aws_lambda_function.power.arn
#   input = jsonencode({ action = "stop" })
# }
# resource "aws_lambda_permission" "events_stop" {
#   count         = var.enable_schedule ? 1 : 0
#   statement_id  = "AllowEventsStop"
#   action        = "lambda:InvokeFunction"
#   function_name = aws_lambda_function.power.function_name
#   principal     = "events.amazonaws.com"
#   source_arn    = aws_cloudwatch_event_rule.stop_nightly[0].arn
# }

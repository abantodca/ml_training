data "archive_file" "notifier" {
  type        = "zip"
  source_dir  = "${var.lambdas_src_dir}/notifier"
  output_path = "${path.module}/.build/notifier.zip"
}

resource "aws_iam_role" "notifier" {
  name = "${var.project}-notifier"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "notifier_basic" {
  role       = aws_iam_role.notifier.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "notifier_publish" {
  name = "sns-publish"
  role = aws_iam_role.notifier.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sns:Publish"]
      Resource = var.sns_topic_arn
    }]
  })
}

resource "aws_cloudwatch_log_group" "notifier" {
  name              = "/aws/lambda/${var.project}-notifier"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "notifier" {
  function_name    = "${var.project}-notifier"
  role             = aws_iam_role.notifier.arn
  filename         = data.archive_file.notifier.output_path
  source_code_hash = data.archive_file.notifier.output_base64sha256
  runtime          = "python3.13"
  handler          = "notifier.lambda_handler"
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      TOPIC_ARN = var.sns_topic_arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.notifier]
}

# Trigger: cualquier transición de jobs Batch a SUCCEEDED o FAILED.
resource "aws_cloudwatch_event_rule" "batch_state" {
  name  = "${var.project}-batch-state"
  state = "ENABLED"
  event_pattern = jsonencode({
    source      = ["aws.batch"]
    detail-type = ["Batch Job State Change"]
    detail      = { status = ["SUCCEEDED", "FAILED"] }
  })
}

resource "aws_cloudwatch_event_target" "batch_state" {
  rule = aws_cloudwatch_event_rule.batch_state.name
  arn  = aws_lambda_function.notifier.arn
}

resource "aws_lambda_permission" "batch_state" {
  statement_id  = "eb-batch-state"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.batch_state.arn
}
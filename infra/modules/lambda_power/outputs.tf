output "function_name" {
  value       = aws_lambda_function.power.function_name
  description = "Nombre invocable de la Lambda (lo necesita el Taskfile)"
}

output "function_arn" {
  value       = aws_lambda_function.power.arn
  description = "ARN de la Lambda"
}

output "log_group" {
  value       = aws_cloudwatch_log_group.lambda.name
  description = "Log group de la Lambda en CloudWatch"
}

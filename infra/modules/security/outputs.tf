output "mlflow_sg_id" {
  value       = aws_security_group.mlflow.id
  description = "SG del MLflow server"
}

output "training_sg_id" {
  value       = aws_security_group.training.id
  description = "SG del training node"
}

output "s3_bucket" {
  description = "Bucket S3 con raw data + MLflow artifacts"
  value       = module.storage.bucket_name
}

output "aws_region_used" {
  description = "Region AWS efectiva (igual a var.aws_region)"
  value       = var.aws_region
}

# ----------- SSH (auto-managed key) -----------
output "ssh_private_key_path" {
  description = "Ruta absoluta al .pem (auto-generado si create_ssh_key=true, perms 0400)"
  value = (
    var.create_ssh_key
    ? abspath(local_sensitive_file.pem[0].filename)
    : pathexpand(replace(var.ssh_public_key_path, ".pub", ""))
  )
}

# ----------- MLflow server -----------
output "mlflow_eip" {
  description = "Elastic IP publica del MLflow server (estable)"
  value       = module.mlflow.public_ip
}

output "mlflow_public_dns" {
  description = "DNS publico (resuelve a la EIP)"
  value       = module.mlflow.public_dns
}

output "mlflow_url" {
  description = "URL de la UI de MLflow (esperar ~2 min tras apply para que arranque)"
  value       = "http://${module.mlflow.public_ip}:5000"
}

output "mlflow_private_ip" {
  description = "IP privada del MLflow server (la que usa la EC2 de training)"
  value       = module.mlflow.private_ip
}

# ----------- Training node -----------
output "training_eip" {
  description = "Elastic IP publica del training node (estable)"
  value       = module.training.public_ip
}

output "training_public_dns" {
  description = "DNS publico del training node"
  value       = module.training.public_dns
}

# ----------- Lambda power manager -----------
output "power_lambda_name" {
  description = "Nombre de la Lambda. Lo lee `task infra:env-export` -> .env.infra"
  value       = module.power.function_name
}

output "power_lambda_log_group" {
  description = "Log group de la Lambda (CloudWatch)"
  value       = module.power.log_group
}

# ----------- Helpers para copiar -----------
output "ssh_command_mlflow" {
  description = "Comando SSH listo para copiar"
  value = format(
    "ssh -i %s ubuntu@%s",
    var.create_ssh_key ? abspath(local_sensitive_file.pem[0].filename) : var.ssh_public_key_path,
    module.mlflow.public_ip,
  )
}

output "ssh_command_training" {
  description = "Comando SSH listo para copiar"
  value = format(
    "ssh -i %s ubuntu@%s",
    var.create_ssh_key ? abspath(local_sensitive_file.pem[0].filename) : var.ssh_public_key_path,
    module.training.public_ip,
  )
}

output "env_block_for_dotenv" {
  description = "Bloque .env listo para pegar (manual). Mejor: `task infra:env-export`."
  value       = <<-EOT
    AWS_REGION=${var.aws_region}
    S3_BUCKET=${module.storage.bucket_name}
    TRAINING_HOST=${module.training.public_ip}
    MLFLOW_HOST=${module.mlflow.public_ip}
    MLFLOW_TRACKING_URI=http://${module.mlflow.public_ip}:5000
    LAMBDA_POWER_NAME=${module.power.function_name}
    SSH_KEY=${var.create_ssh_key ? abspath(local_sensitive_file.pem[0].filename) : var.ssh_public_key_path}
  EOT
}

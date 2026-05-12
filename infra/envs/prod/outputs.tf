# infra/envs/prod/outputs.tf
# Los outputs se anaden a medida que cada modulo de Sec 4.X se construye.
# Cada subseccion (4.1-4.9) lista las lineas que hay que apendizar aca.

output "data_bucket" { value = module.storage.data_bucket }
output "artifacts_bucket" { value = module.storage.artifacts_bucket }
output "ecr_trainer_repo_url" { value = module.storage.ecr_trainer_url }
output "mlflow_url" { value = "http://${module.mlflow.alb_dns_name}" }
output "batch_job_queue_spot" { value = module.batch.job_queue_spot }
output "batch_job_queue_od"   { value = module.batch.job_queue_ondemand }
# Necesario para los comandos del runbook Sec 8.1/8.2.
output "batch_job_definition" { value = module.batch.job_definition_name }
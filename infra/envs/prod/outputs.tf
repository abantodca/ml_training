# infra/envs/prod/outputs.tf
# Los outputs se anaden a medida que cada modulo de Sec 4.X se construye.
# Cada subseccion (4.1-4.9) lista las lineas que hay que apendizar aca.

output "data_bucket"          { value = module.storage.data_bucket }
output "artifacts_bucket"     { value = module.storage.artifacts_bucket }
output "ecr_trainer_repo_url" { value = module.storage.ecr_trainer_url }
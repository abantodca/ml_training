# infra/envs/prod/main.tf
# Composicion del entorno prod. Los `module "X" { ... }` se anaden al final
# de este archivo a medida que construis cada modulo en Sec 4.1-4.9.
# Orden topologico de dependencia (NO alterar):
#   network -> storage -> mlflow -> reports -> batch -> monitoring -> lambdas -> scheduler

module "network" {
  source   = "../../modules/network"
  project  = var.project
  vpc_cidr = var.vpc_cidr
}

module "storage" {
  source  = "../../modules/storage"
  project = var.project
}

module "mlflow" {
  source                   = "../../modules/mlflow"
  project                  = var.project
  vpc_id                   = module.network.vpc_id
  public_subnet_ids        = module.network.public_subnet_ids
  private_subnet_ids       = module.network.private_subnet_ids
  sg_alb_id                = module.network.sg_alb_id
  sg_mlflow_id             = module.network.sg_mlflow_id
  sg_rds_id                = module.network.sg_rds_id
  rds_instance_class       = var.rds_instance_class
  rds_allocated_storage_gb = 20
  mlflow_image             = var.mlflow_image
  artifacts_bucket         = module.storage.artifacts_bucket
  log_retention_days       = var.log_retention_days
}
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
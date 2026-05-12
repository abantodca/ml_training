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

module "reports" {
  source               = "../../modules/reports"
  project              = var.project
  vpc_id               = module.network.vpc_id
  private_subnet_ids   = module.network.private_subnet_ids
  sg_alb_id            = module.network.sg_alb_id
  ecs_cluster_id       = module.mlflow.cluster_id
  alb_listener_arn     = module.mlflow.alb_listener_arn
  artifacts_bucket     = module.storage.artifacts_bucket
  artifacts_bucket_arn = module.storage.artifacts_bucket_arn
  ecr_reports_url      = module.storage.ecr_reports_url
  log_retention_days   = var.log_retention_days
}

module "batch" {
  source               = "../../modules/batch"
  project              = var.project
  private_subnet_ids   = module.network.private_subnet_ids
  sg_batch_id          = module.network.sg_batch_id
  ecr_trainer_url      = module.storage.ecr_trainer_url
  trainer_image_tag    = var.trainer_image_tag
  spot_bid_percentage  = var.spot_bid_percentage
  tracking_uri         = module.mlflow.tracking_uri
  artifacts_bucket     = module.storage.artifacts_bucket
  artifacts_bucket_arn = module.storage.artifacts_bucket_arn
  data_bucket          = module.storage.data_bucket
  data_bucket_arn      = module.storage.data_bucket_arn
  job_attempt_seconds  = var.job_attempt_seconds
  log_retention_days   = var.log_retention_days
}

module "monitoring" {
  source               = "../../modules/monitoring"
  project              = var.project
  alert_email          = var.alert_email
  batch_job_queue_name = module.batch.job_queue_spot
  alb_arn              = module.mlflow.alb_arn
  tg_arn               = module.mlflow.tg_arn
  mape_alarm_threshold = var.mape_alarm_threshold
}

module "lambdas" {
  source                 = "../../modules/lambdas"
  project                = var.project
  job_queue_spot_arn     = module.batch.job_queue_spot_arn
  job_queue_ondemand_arn = module.batch.job_queue_ondemand_arn
  job_definition_name    = module.batch.job_definition_name
  default_tuning         = var.default_tuning
  data_bucket            = module.storage.data_bucket
  varieties_allowed      = var.varieties_allowed
  sns_topic_arn          = module.monitoring.sns_topic_arn
  log_retention_days     = var.log_retention_days
  lambdas_src_dir        = "${path.module}/../../lambdas"
}

module "scheduler" {
  source                    = "../../modules/scheduler"
  project                   = var.project
  ecs_cluster_name          = module.mlflow.cluster_name
  ecs_service_name_mlflow   = module.mlflow.service_name
  ecs_service_name_reports  = module.reports.service_name
  rds_instance_id           = module.mlflow.rds_instance_id
  batch_queue_spot_name     = module.batch.job_queue_spot
  batch_queue_ondemand_name = module.batch.job_queue_ondemand
  work_end_hour_local       = 12               # 12:00 PM PET (decision 3 de Sec 0.3)
  lambdas_src_dir           = "${path.module}/../../lambdas"
}
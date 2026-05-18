variable "project" { type = string }
variable "ecs_cluster_name" { type = string }
variable "ecs_service_name_mlflow" { type = string }
variable "ecs_service_name_reports" { type = string }
variable "rds_instance_id" { type = string }
variable "work_start_hour_local" { type = number }
variable "work_end_hour_local" { type = number }
variable "tz_offset_hours" {
  type    = number
  default = -5 # PET (Peru)
}
variable "workdays_cron" {
  type    = string
  default = "MON,WED,FRI" # Patch 13.1: solo L/Mi/V (antes: "MON-FRI")
}
variable "log_retention_days" { type = number }
variable "lambdas_src_dir" { type = string }

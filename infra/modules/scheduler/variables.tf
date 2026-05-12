variable "project" {
  type = string
}

variable "ecs_cluster_name" {
  type = string
}

variable "ecs_service_name_mlflow" {
  type = string
}

variable "ecs_service_name_reports" {
  type = string
}

variable "rds_instance_id" {
  type = string
}

# Para chequear jobs activos pre-shutdown.
variable "batch_queue_spot_name" {
  type = string
}

variable "batch_queue_ondemand_name" {
  type = string
}

# Perú (PET = UTC-5).
variable "tz_offset_hours" {
  type    = number
  default = -5
}

variable "work_start_hour_local" {
  type    = number
  default = 8
}

# 12:00 PM PET.
variable "work_end_hour_local" {
  type    = number
  default = 12
}

variable "workdays_cron" {
  type    = string
  default = "MON-FRI"
}

variable "lambdas_src_dir" {
  type = string
}
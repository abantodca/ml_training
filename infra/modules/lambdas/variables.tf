variable "project" {
  type = string
}

variable "job_queue_spot_arn" {
  type = string
}

variable "job_queue_ondemand_arn" {
  type = string
}

variable "job_definition_name" {
  type = string
}

variable "default_tuning" {
  type    = string
  default = "prod"
}

variable "data_bucket" {
  type = string
}

# Whitelist para dispatcher.py.
variable "varieties_allowed" {
  type = list(string)
}

variable "sns_topic_arn" {
  type = string
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "lambdas_src_dir" {
  type = string
}
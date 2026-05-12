variable "project" {
  type = string
}

variable "alert_email" {
  type = string
}

variable "batch_job_queue_name" {
  type = string
}

variable "alb_arn" {
  type = string
}

variable "tg_arn" {
  type = string
}

variable "mape_alarm_threshold" {
  type    = number
  default = 25
}
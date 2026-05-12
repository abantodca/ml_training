variable "project" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "sg_batch_id" {
  type = string
}

variable "ecr_trainer_url" {
  type = string
}

variable "trainer_image_tag" {
  type    = string
  default = "latest"
}

variable "spot_bid_percentage" {
  type    = number
  default = 70
}

# 2 jobs paralelos c6i.2xlarge
variable "spot_max_vcpus" {
  type    = number
  default = 16
}

variable "ondemand_max_vcpus" {
  type    = number
  default = 16
}

variable "instance_type" {
  type    = string
  default = "c6i.2xlarge"
}

variable "tracking_uri" {
  type = string
}

variable "artifacts_bucket" {
  type = string
}

variable "artifacts_bucket_arn" {
  type = string
}

variable "data_bucket" {
  type = string
}

variable "data_bucket_arn" {
  type = string
}

variable "job_attempt_seconds" {
  type    = number
  default = 28800
}

variable "log_retention_days" {
  type    = number
  default = 30
}
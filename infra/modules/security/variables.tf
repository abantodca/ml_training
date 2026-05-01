variable "name_prefix" {
  description = "Prefijo para nombres y tags"
  type        = string
}

variable "vpc_id" {
  description = "ID de la VPC donde viven los SGs"
  type        = string
}

variable "admin_cidr" {
  description = "Tu IP /32 admin (SSH y UI MLflow)"
  type        = string
}

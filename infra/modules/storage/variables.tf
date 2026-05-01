variable "bucket_name" {
  description = "Nombre globalmente unico del bucket S3"
  type        = string
}

variable "mlflow_versions_retain_days" {
  description = "Dias antes de expirar versiones viejas de mlflow-artifacts/"
  type        = number
  default     = 30
}

variable "code_versions_retain_days" {
  description = "Dias antes de expirar versiones viejas de code/"
  type        = number
  default     = 14
}

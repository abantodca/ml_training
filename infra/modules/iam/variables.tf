variable "name_prefix" {
  description = "Prefijo para nombres y tags"
  type        = string
}

variable "s3_bucket_arn" {
  description = "ARN del bucket S3 al que se da acceso R/W"
  type        = string
}

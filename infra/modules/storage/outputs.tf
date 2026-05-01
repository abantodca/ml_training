output "bucket_id" {
  value       = aws_s3_bucket.data.id
  description = "ID/nombre del bucket"
}

output "bucket_arn" {
  value       = aws_s3_bucket.data.arn
  description = "ARN del bucket (usado por IAM)"
}

output "bucket_name" {
  value       = aws_s3_bucket.data.bucket
  description = "Nombre del bucket"
}

output "data_bucket" {
  value = aws_s3_bucket.data.id
}

output "data_bucket_arn" {
  value = aws_s3_bucket.data.arn
}

output "artifacts_bucket" {
  value = aws_s3_bucket.mlflow.id
}

output "artifacts_bucket_arn" {
  value = aws_s3_bucket.mlflow.arn
}

output "ecr_trainer_url" {
  value = aws_ecr_repository.trainer.repository_url
}

output "ecr_reports_url" {
  value = aws_ecr_repository.reports.repository_url
}
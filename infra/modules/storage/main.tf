resource "aws_s3_bucket" "data" {
  bucket = var.bucket_name
  # `force_destroy` permite que `terraform destroy` borre el bucket aunque
  # contenga objetos y versiones (versioning esta enabled abajo). Sin esto,
  # `task infra:destroy` falla con BucketNotEmpty y obliga a un `aws s3 rm
  # --recursive` manual previo. La intencion del task es dejar la AWS limpia,
  # asi que activamos force_destroy. Si llegas a tener data critica en
  # mlflow-artifacts/ o raw/, hace backup ANTES de correr infra:destroy.
  force_destroy = true
  tags          = { Name = var.bucket_name }
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Lifecycle: control de costos y limpieza automatica.
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  # versiones viejas de artifacts MLflow
  rule {
    id     = "expire-old-mlflow-artifact-versions"
    status = "Enabled"
    filter {
      prefix = "mlflow-artifacts/"
    }
    noncurrent_version_expiration {
      noncurrent_days = var.mlflow_versions_retain_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # versiones viejas del tar.gz del codigo
  rule {
    id     = "expire-old-code-archives"
    status = "Enabled"
    filter {
      prefix = "code/"
    }
    noncurrent_version_expiration {
      noncurrent_days = var.code_versions_retain_days
    }
  }

  # raw data: solo limpia uploads abortados
  rule {
    id     = "raw-cleanup-aborted"
    status = "Enabled"
    filter {
      prefix = "raw/"
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.data]
}

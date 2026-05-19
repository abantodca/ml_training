# Patch 13.5: rol que el repo consumer (FastAPI/Streamlit) asume via OIDC
# para descargar artifacts (modelos) desde S3 read-only.

resource "aws_iam_role" "consumer" {
  name = "${var.project}-consumer"
  assume_role_policy = templatefile("${path.module}/../_shared/assume-github-oidc.json.tftpl", {
    provider_arn = var.consumer_oidc_arn
    org          = var.consumer_org
    repo         = var.consumer_repo
  })
}

resource "aws_iam_role_policy" "consumer" {
  role = aws_iam_role.consumer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.artifacts_bucket_arn, "${var.artifacts_bucket_arn}/*"]
      }
    ]
  })
}

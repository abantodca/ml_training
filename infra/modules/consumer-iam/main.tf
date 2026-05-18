data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Patch 13.5: rol que el repo consumer (FastAPI/Streamlit) asume via OIDC
# para descargar artifacts (modelos) desde S3 read-only.

resource "aws_iam_role" "consumer" {
  name = "${var.project}-consumer"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.consumer_oidc_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${var.consumer_org}/${var.consumer_repo}:*"
        }
      }
    }]
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

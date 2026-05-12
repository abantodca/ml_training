data "aws_region"          "current" {}
data "aws_caller_identity" "current" {}

# ─── Instance profile (host EC2 de Batch) ────────────────────────────────────

resource "aws_iam_role" "instance" {
  name = "${var.project}-batch-instance"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "instance" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.project}-batch-instance"
  role = aws_iam_role.instance.name
}

# ─── Execution role (lo asume el container al arrancar) ──────────────────────

resource "aws_iam_role" "exec" {
  name = "${var.project}-batch-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "exec" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ─── Job role (lo que hace el código del trainer) ────────────────────────────

resource "aws_iam_role" "job" {
  name = "${var.project}-batch-job"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "job_s3" {
  name = "s3-data-and-artifacts"
  role = aws_iam_role.job.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [var.data_bucket_arn, var.artifacts_bucket_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = ["${var.data_bucket_arn}/*", "${var.artifacts_bucket_arn}/*"]
      },
    ]
  })
}

resource "aws_iam_role_policy" "job_cw_metrics" {
  name = "cw-metrics-emit"
  role = aws_iam_role.job.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = ["cloudwatch:PutMetricData"]
      Resource  = "*"
      Condition = { StringEquals = { "cloudwatch:namespace" = "MLTraining" } }
    }]
  })
}
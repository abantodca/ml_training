resource "aws_cloudwatch_log_group" "batch" {
  name              = "/aws/batch/${var.project}"
  retention_in_days = var.log_retention_days
}

resource "aws_batch_compute_environment" "spot" {
  compute_environment_name = "${var.project}-ce-spot"
  type                     = "MANAGED"
  state                    = "ENABLED"

  compute_resources {
    type                = "EC2"
    allocation_strategy = "BEST_FIT_PROGRESSIVE"
    bid_percentage      = var.spot_bid_percentage
    min_vcpus           = 0
    max_vcpus           = var.spot_max_vcpus
    desired_vcpus       = 0
    instance_type       = [var.instance_type]
    subnets             = var.private_subnet_ids
    security_group_ids  = [var.sg_batch_id]
    instance_role       = aws_iam_instance_profile.instance.arn
    spot_iam_fleet_role = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-service-role/spotfleet.amazonaws.com/AWSServiceRoleForEC2SpotFleet"
  }
}

resource "aws_batch_compute_environment" "ondemand" {
  compute_environment_name = "${var.project}-ce-ondemand"
  type                     = "MANAGED"
  state                    = "ENABLED"

  compute_resources {
    type                = "EC2"
    allocation_strategy = "BEST_FIT_PROGRESSIVE"
    min_vcpus           = 0
    max_vcpus           = var.ondemand_max_vcpus
    desired_vcpus       = 0
    instance_type       = [var.instance_type]
    subnets             = var.private_subnet_ids
    security_group_ids  = [var.sg_batch_id]
    instance_role       = aws_iam_instance_profile.instance.arn
  }
}

resource "aws_batch_job_queue" "spot" {
  name     = "${var.project}-queue"
  state    = "ENABLED"
  priority = 100

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.spot.arn
  }
}

resource "aws_batch_job_queue" "ondemand" {
  name     = "${var.project}-queue-ondemand"
  state    = "ENABLED"
  priority = 100

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.ondemand.arn
  }
}

resource "aws_batch_job_definition" "trainer" {
  name                  = var.project
  type                  = "container"
  platform_capabilities = ["EC2"]

  retry_strategy {
    attempts = 2
  }

  # 8h cubre prod_xl
  timeout {
    attempt_duration_seconds = var.job_attempt_seconds
  }

  container_properties = jsonencode({
    image            = "${var.ecr_trainer_url}:${var.trainer_image_tag}"
    command          = ["--varieties", "POP", "--tuning", "prod"] # default; el dispatcher lo override-a
    executionRoleArn = aws_iam_role.exec.arn
    jobRoleArn       = aws_iam_role.job.arn
    resourceRequirements = [
      { type = "VCPU",   value = "8" },
      { type = "MEMORY", value = "15000" },
    ]
    environment = [
      { name = "MLFLOW_TRACKING_URI", value = var.tracking_uri },
      { name = "S3_ARTIFACTS_BUCKET", value = var.artifacts_bucket },
      { name = "S3_ARTIFACTS_PREFIX", value = "artifacts" },
      { name = "S3_REPORTS_PREFIX",   value = "reports" },
      { name = "AWS_DEFAULT_REGION",  value = data.aws_region.current.name },
      { name = "EMIT_CW_METRICS",     value = "1" }, # activa metric custom para alarma de MAPE
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.batch.name
        awslogs-region        = data.aws_region.current.name
        awslogs-stream-prefix = "job"
      }
    }
  })
}
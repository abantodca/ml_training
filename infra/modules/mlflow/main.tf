data "aws_region" "current" {}

# ─── RDS Postgres + Secret ────────────────────────────────────────────────────

resource "random_password" "db" {
  length           = 24
  special          = true
  override_special = "!#$%&*-_=+" # evitar @ y espacios para la URI de MLflow
}

resource "aws_secretsmanager_secret" "db" {
  name                    = "${var.project}/mlflow/db"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id     = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({ username = "mlflow", password = random_password.db.result })
}

resource "aws_db_subnet_group" "mlflow" {
  name       = "${var.project}-mlflow"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_instance" "mlflow" {
  identifier              = "${var.project}-mlflow"
  engine                  = "postgres"
  engine_version          = "15.7"
  instance_class          = var.rds_instance_class
  allocated_storage       = var.rds_allocated_storage_gb
  storage_type            = "gp3"
  storage_encrypted       = true
  db_name                 = "mlflow"
  username                = "mlflow"
  password                = random_password.db.result
  vpc_security_group_ids  = [var.sg_rds_id]
  db_subnet_group_name    = aws_db_subnet_group.mlflow.name
  multi_az                = false
  publicly_accessible     = false
  backup_retention_period = 7
  skip_final_snapshot     = true
  apply_immediately       = true
}

# ─── ALB (público) + Target Group + Listener ─────────────────────────────────

resource "aws_lb" "mlflow" {
  name               = "${var.project}-mlflow"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [var.sg_alb_id]
  subnets            = var.public_subnet_ids
}

resource "aws_lb_target_group" "mlflow" {
  name        = "${var.project}-mlflow"
  port        = 5000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  health_check {
    path     = "/health"
    matcher  = "200"
    interval = 30
  }
}

resource "aws_lb_listener" "mlflow" {
  load_balancer_arn = aws_lb.mlflow.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.mlflow.arn
  }
}

# ─── ECS Cluster + Task Definition + Service ─────────────────────────────────

resource "aws_ecs_cluster" "this" {
  name = "${var.project}-cluster"
}

resource "aws_cloudwatch_log_group" "mlflow" {
  name              = "/aws/ecs/${var.project}-mlflow"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "ecs_exec" {
  name = "${var.project}-ecs-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_exec" {
  role       = aws_iam_role.ecs_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "mlflow_task" {
  name = "${var.project}-mlflow-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "mlflow_task_s3" {
  name = "s3-artifacts"
  role = aws_iam_role.mlflow_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
      Resource = [
        "arn:aws:s3:::${var.artifacts_bucket}",
        "arn:aws:s3:::${var.artifacts_bucket}/*",
      ]
    }]
  })
}

locals {
  alb_dns        = aws_lb.mlflow.dns_name
  allowed_hosts  = "${local.alb_dns},${local.alb_dns}:*,localhost,localhost:*,127.0.0.1,127.0.0.1:*"
  backend_db_uri = "postgresql://mlflow:${random_password.db.result}@${aws_db_instance.mlflow.endpoint}/mlflow"
  artifact_root  = "s3://${var.artifacts_bucket}/artifacts"
}

resource "aws_ecs_task_definition" "mlflow" {
  family                   = "${var.project}-mlflow"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_exec.arn
  task_role_arn            = aws_iam_role.mlflow_task.arn

  container_definitions = jsonencode([{
    name      = "mlflow"
    image     = var.mlflow_image
    essential = true
    portMappings = [{ containerPort = 5000, protocol = "tcp" }]
    command = [
      "mlflow", "server",
      "--host", "0.0.0.0", "--port", "5000",
      "--allowed-hosts", local.allowed_hosts,
      "--backend-store-uri", local.backend_db_uri,
      "--default-artifact-root", local.artifact_root,
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.mlflow.name
        awslogs-region        = data.aws_region.current.name
        awslogs-stream-prefix = "mlflow"
      }
    }
  }])
}

resource "aws_ecs_service" "mlflow" {
  name            = "${var.project}-mlflow"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.mlflow.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.sg_mlflow_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.mlflow.arn
    container_name   = "mlflow"
    container_port   = 5000
  }

  depends_on = [aws_lb_listener.mlflow]
}
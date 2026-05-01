# Dos SGs con dependencia ciclica: el SG de mlflow permite ingress 5000
# desde el SG de training. Ambos viven en este modulo para evitar pasar
# IDs entre modulos en orden circular.

resource "aws_security_group" "mlflow" {
  name        = "${var.name_prefix}-mlflow-sg"
  description = "MLflow server: 5000 desde training, SSH desde admin"
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH from admin"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }

  ingress {
    description = "MLflow UI from admin (lectura)"
    from_port   = 5000
    to_port     = 5000
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }

  ingress {
    description     = "MLflow API from training node"
    from_port       = 5000
    to_port         = 5000
    protocol        = "tcp"
    security_groups = [aws_security_group.training.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name_prefix}-mlflow-sg" }
}

resource "aws_security_group" "training" {
  name        = "${var.name_prefix}-training-sg"
  description = "Training node: SSH desde admin, sin puertos publicos"
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH from admin"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name_prefix}-training-sg" }
}

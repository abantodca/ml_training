# ============================================================================
# main.tf - composicion de modulos
#
# Cada modulo es una unidad reusable y reemplazable:
#   modules/network        VPC + subred publica + IGW + route table
#   modules/storage        S3 con versioning + encriptacion + lifecycle
#   modules/security       2 Security Groups (mlflow + training, ref ciclica)
#   modules/iam            IAM role + instance profile + politica S3
#   modules/ec2_instance   EC2 generico + EIP + EBS gp3 cifrado
#
# Esta composicion produce:
#   - 1 VPC con 1 subred publica
#   - 1 S3 bucket privado con lifecycle
#   - 2 SGs (mlflow + training)
#   - 1 IAM role compartido por las EC2
#   - 2 EC2 (mlflow t3.medium + training t3.large), cada una con su EIP
# ============================================================================

# ---------- SSH key pair ----------
# Si create_ssh_key=true (default), Terraform genera el par y guarda el .pem
# local con permisos 0400, listo para SSH. Si false, usa la .pub existente.

resource "tls_private_key" "ssh" {
  count     = var.create_ssh_key ? 1 : 0
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "local_sensitive_file" "pem" {
  count           = var.create_ssh_key ? 1 : 0
  filename        = "${path.module}/${var.project_name}-key.pem"
  content         = tls_private_key.ssh[0].private_key_pem
  file_permission = "0400"
}

resource "aws_key_pair" "main" {
  key_name = "${var.project_name}-key"
  public_key = (
    var.create_ssh_key
    ? tls_private_key.ssh[0].public_key_openssh
    : file(pathexpand(var.ssh_public_key_path))
  )
}

# NOTA seguridad: el password de Postgres NO se inyecta desde Terraform.
# Cloud-init lo genera localmente con `openssl rand` en /etc/mlflow/db.env
# (perms 0600). Asi nunca aparece en user_data ni en el state de Terraform
# (que es visible para cualquiera con acceso al state remoto).

# ----------------------- Modulos -----------------------

module "network" {
  source             = "./modules/network"
  name_prefix        = var.project_name
  vpc_cidr           = var.vpc_cidr
  public_subnet_cidr = var.public_subnet_cidr
}

module "storage" {
  source                      = "./modules/storage"
  bucket_name                 = var.s3_bucket_name
  mlflow_versions_retain_days = 30
  code_versions_retain_days   = 14
}

module "security" {
  source      = "./modules/security"
  name_prefix = var.project_name
  vpc_id      = module.network.vpc_id
  admin_cidr  = var.admin_cidr
}

module "iam" {
  source        = "./modules/iam"
  name_prefix   = var.project_name
  s3_bucket_arn = module.storage.bucket_arn
}

# ----------------------- EC2: MLflow server -----------------------

module "mlflow" {
  source                = "./modules/ec2_instance"
  name                  = "${var.project_name}-mlflow"
  instance_type         = var.instance_type_mlflow
  subnet_id             = module.network.public_subnet_id
  security_group_ids    = [module.security.mlflow_sg_id]
  key_name              = aws_key_pair.main.key_name
  instance_profile_name = module.iam.instance_profile_name
  ebs_size_gb           = var.ebs_size_gb_mlflow
  tags                  = { Role = "mlflow-server" }

  # MLflow guarda metadata en Postgres self-hosted sobre el EBS root.
  # Editar el cloud-init NO debe destruir la instancia (perderiamos toda la
  # historia de runs). Si el script cambia y necesitas re-aplicar, hazlo
  # manualmente via SSH o destruye/recrea con respaldo previo.
  user_data_replace_on_change       = false
  root_volume_delete_on_termination = false

  user_data = templatefile("${path.module}/cloud-init/mlflow_server.sh", {
    s3_bucket = module.storage.bucket_name
    region    = var.aws_region
  })
}

# ----------------------- EC2: Training node -----------------------

# ----------------------- Lambda power manager -----------------------
# Apaga/prende las EC2 del proyecto via tag. Idempotente.
module "power" {
  source       = "./modules/lambda_power"
  name_prefix  = var.project_name
  project_name = var.project_name
}

module "training" {
  source                = "./modules/ec2_instance"
  name                  = "${var.project_name}-training"
  instance_type         = var.instance_type_training
  subnet_id             = module.network.public_subnet_id
  security_group_ids    = [module.security.training_sg_id]
  key_name              = aws_key_pair.main.key_name
  instance_profile_name = module.iam.instance_profile_name
  ebs_size_gb           = var.ebs_size_gb_training
  tags                  = { Role = "training-node" }

  user_data = templatefile("${path.module}/cloud-init/training_node.sh", {
    s3_bucket           = module.storage.bucket_name
    region              = var.aws_region
    mlflow_private_ip   = module.mlflow.private_ip
    code_archive_s3_uri = "s3://${module.storage.bucket_name}/${var.code_archive_s3_key}"
    git_repo_url        = var.git_repo_url
  })

  depends_on = [module.mlflow]
}

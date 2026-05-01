variable "aws_region" {
  description = "Region AWS donde se desplegara todo"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefijo de nombres y tags (a-z 0-9 -) para todos los recursos"
  type        = string
  default     = "ml-training"
}

variable "environment" {
  description = "Nombre del ambiente (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "owner" {
  description = "Etiqueta Owner (email o usuario responsable)"
  type        = string
  default     = "data-team"
}

# ----------------------- Red -----------------------

variable "vpc_cidr" {
  description = "CIDR de la VPC propia"
  type        = string
  default     = "10.20.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR de la subred publica donde viven las EC2"
  type        = string
  default     = "10.20.1.0/24"
}

variable "admin_cidr" {
  description = "Tu IP /32 para SSH (ej. 200.10.10.10/32). Cambialo SI o SI."
  type        = string
  default     = "0.0.0.0/0"
  # nota: 0.0.0.0/0 abre SSH al mundo. Solo usalo para test rapido.

  validation {
    condition     = !contains(["0.0.0.0/0", "::/0"], var.admin_cidr)
    error_message = "admin_cidr no puede ser 0.0.0.0/0 (SSH abierto al mundo). Pon tu IP publica en formato /32 en terraform.tfvars (ej. 200.10.10.10/32). Si necesitas test rapido, comenta esta validacion temporalmente."
  }
}

# ----------------------- EC2 -----------------------

variable "create_ssh_key" {
  description = "Si true, Terraform GENERA el keypair y guarda el .pem local con perm 0400. Si false, usa ssh_public_key_path."
  type        = bool
  default     = true
}

variable "ssh_public_key_path" {
  description = "Solo se usa cuando create_ssh_key=false. Ruta al .pub existente."
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}

variable "instance_type_mlflow" {
  description = "Tipo EC2 para el MLflow server (poco CPU, algo de RAM)"
  type        = string
  default     = "t3.medium"
}

variable "instance_type_training" {
  description = "Tipo EC2 para entrenamiento (CPU bound)"
  type        = string
  default     = "t3.large"
}

variable "ebs_size_gb_mlflow" {
  description = "Tamano del disco para el MLflow server (sqlite + artifacts cache)"
  type        = number
  default     = 30
}

variable "ebs_size_gb_training" {
  description = "Tamano del disco para training (datasets + artifacts intermedios)"
  type        = number
  default     = 50
}

# ----------------------- S3 -----------------------

variable "s3_bucket_name" {
  description = "Nombre del bucket S3 (debe ser globalmente unico)"
  type        = string
  default     = "ml-poc-training-data"
}

variable "code_archive_s3_key" {
  description = "Key S3 del tar.gz del codigo (lo sube `task deploy:upload-code`)"
  type        = string
  default     = "code/ml_training.tar.gz"
}

# ----------------------- Bootstrap -----------------------

variable "git_repo_url" {
  description = "URL HTTPS del repo a clonar en la EC2 de training (vacio = no clonar)"
  type        = string
  default     = ""
}

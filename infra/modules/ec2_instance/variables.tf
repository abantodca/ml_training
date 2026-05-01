variable "name" {
  description = "Nombre logico (entra al tag Name)"
  type        = string
}

variable "instance_type" {
  description = "Tipo EC2 (ej. t3.medium, t3.large)"
  type        = string
}

variable "subnet_id" {
  description = "ID de la subred"
  type        = string
}

variable "security_group_ids" {
  description = "Lista de SG IDs a asociar"
  type        = list(string)
}

variable "key_name" {
  description = "Nombre del aws_key_pair"
  type        = string
}

variable "instance_profile_name" {
  description = "Nombre del instance profile IAM"
  type        = string
}

variable "user_data" {
  description = "Cloud-init script (string)"
  type        = string
}

variable "ebs_size_gb" {
  description = "Tamano del root EBS"
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags adicionales"
  type        = map(string)
  default     = {}
}

variable "user_data_replace_on_change" {
  description = "Si true, cambiar user_data REEMPLAZA la instancia (destruye state local). Para nodos stateless (training) deja true; para nodos con datos en disco (mlflow/postgres) deja false para que no pierdas la BD al editar el cloud-init."
  type        = bool
  default     = true
}

variable "root_volume_delete_on_termination" {
  description = "Si true, el EBS root se borra al terminar la instancia. Para mlflow/postgres ponlo en false para conservar la BD si la instancia muere."
  type        = bool
  default     = true
}

variable "name_prefix" {
  description = "Prefijo para nombres y tags"
  type        = string
}

variable "project_name" {
  description = "Valor exacto del tag Project que filtra las EC2 elegibles"
  type        = string
}

variable "enable_schedule" {
  description = "Si true, agrega un cron EventBridge para apagar de noche"
  type        = bool
  default     = false
}

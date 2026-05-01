variable "name_prefix" {
  description = "Prefijo para nombres y tags"
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR de la VPC"
  type        = string
}

variable "public_subnet_cidr" {
  description = "CIDR de la subred publica"
  type        = string
}

variable "project" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

# Si está vacía, se eligen las 2 primeras AZs de la región.
variable "azs" {
  type    = list(string)
  default = []
}
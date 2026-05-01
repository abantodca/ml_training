output "vpc_id" {
  value       = aws_vpc.main.id
  description = "ID de la VPC"
}

output "public_subnet_id" {
  value       = aws_subnet.public.id
  description = "ID de la subred publica"
}

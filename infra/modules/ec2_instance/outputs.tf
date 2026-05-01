output "instance_id" {
  value       = aws_instance.this.id
  description = "ID de la EC2"
}

output "private_ip" {
  value       = aws_instance.this.private_ip
  description = "IP privada (talk dentro de la VPC)"
}

output "public_ip" {
  value       = aws_eip.this.public_ip
  description = "Elastic IP publica (estable)"
}

output "public_dns" {
  value       = aws_eip.this.public_dns
  description = "DNS publico de la Elastic IP"
}

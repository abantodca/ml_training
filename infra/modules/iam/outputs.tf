output "instance_profile_name" {
  value       = aws_iam_instance_profile.ec2.name
  description = "Nombre del instance profile a inyectar en aws_instance"
}

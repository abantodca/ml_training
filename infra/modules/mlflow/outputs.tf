output "alb_dns_name" {
  value = aws_lb.mlflow.dns_name
}

output "alb_arn" {
  value = aws_lb.mlflow.arn
}

# Consumido por modules/reports/ para agregar listener_rule.
output "alb_listener_arn" {
  value = aws_lb_listener.mlflow.arn
}

output "tg_arn" {
  value = aws_lb_target_group.mlflow.arn
}

output "tracking_uri" {
  value = "http://${aws_lb.mlflow.dns_name}"
}

# Consumido por modules/reports/.
output "cluster_id" {
  value = aws_ecs_cluster.this.id
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "service_name" {
  value = aws_ecs_service.mlflow.name
}

output "rds_instance_id" {
  value = aws_db_instance.mlflow.identifier
}
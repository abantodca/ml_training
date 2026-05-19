# Modulo MLflow: RDS Postgres + ALB + ECS Fargate.
# Recursos divididos por responsabilidad:
#   rds.tf  - random_password + secret + db_instance + subnet group
#   alb.tf  - load balancer + target group + listener
#   iam.tf  - assume policy + exec/task roles
#   ecs.tf  - cluster + service discovery + task def + service + log group
data "aws_region" "current" {}

# `_shared/` — Trust policies compartidos

Documentos de assume-role JSON que se repetian en varios modulos. Cada modulo
los carga con `file()` o `templatefile()` (en lugar de redeclarar el mismo
`data "aws_iam_policy_document"`). Cambio puramente de organizacion: AWS
provider normaliza JSON, asi que `terraform plan` queda no-op.

Files:
- `assume-ecs-tasks.json`      Fargate / ECS task roles (mlflow, reports, batch job role)
- `assume-lambda.json`         Lambda execution roles (dispatcher, notifier, scheduler)
- `assume-ec2.json`            EC2 instance profile (batch compute env)
- `assume-batch-service.json`  AWS Batch service role
- `assume-github-oidc.json.tftpl`  GHA OIDC (cicd + consumer-iam), parametriza provider_arn/org/repo

Si necesitas un nuevo trust nuevo (ej. RDS, EventBridge), agregarlo aqui en
vez de inlinearlo en el modulo.

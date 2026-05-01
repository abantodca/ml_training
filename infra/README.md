# infra/

Infraestructura AWS del proyecto, en Terraform **modular**.

## Layout

```
infra/
├── main.tf                  # composicion: instancia los modulos y conecta outputs->inputs
├── variables.tf             # vars del root (proyecto, region, tipo de EC2, etc.)
├── outputs.tf               # outputs agregados (URLs, EIPs, env_block_for_dotenv)
├── versions.tf              # provider AWS + terraform >= 1.5
├── terraform.tfvars.example # plantilla de variables sensibles
├── cloud-init/
│   ├── mlflow_server.sh     # bootstrap del MLflow server (systemd + sqlite + S3)
│   └── training_node.sh     # bootstrap del training node (venv + .env + Taskfile)
└── modules/                 # reutilizables, cada uno con main/variables/outputs
    ├── network/             # VPC + subnet + IGW + RT
    ├── storage/             # S3 + versioning + encryption + lifecycle
    ├── security/            # 2 SGs (mlflow + training, ref ciclica adentro)
    ├── iam/                 # role + instance profile + policy S3 + CloudWatch
    └── ec2_instance/        # EC2 generico + EIP + EBS gp3 cifrado
```

Beneficios del split:
- **Mantenimiento**: cambiar el lifecycle del S3 vive en `modules/storage/main.tf`, no en un main de 300 lineas.
- **Reuso**: `modules/ec2_instance` se instancia 2 veces con distintos `user_data` (mlflow vs training). Anadir un 3er nodo (e.g. inference) son ~10 lineas en `main.tf`.
- **Testing**: cada modulo se puede importar aisladamente desde otro stack.

## Topología

```
                    +------- admin (tu laptop) -------+
                    | SSH (22) + UI MLflow (5000)     |
                    +----------------+----------------+
                                     |
                       +-------------v-------------+
                       |   VPC 10.20.0.0/16        |
                       |   subnet pub 10.20.1.0/24 |
                       +----+-----------------+----+
                            |                 |
                +-----------v-----+   +-------v---------+
                |  EC2 mlflow     |   |  EC2 training   |
                |  t3.medium      |<--+  t3.large       |
                |  :5000 mlflow   |   |  cron / SSH job |
                |  sqlite + S3    |   |  pulls data S3  |
                +--------+--------+   +--------+--------+
                         |                     |
                         |        S3 bucket    |
                         +---------+-----------+
                                   |
                          +--------v---------+
                          | s3://...         |
                          | raw/             |  <- BD_HISTORICO_ACUMULADO.xlsx
                          | code/            |  <- ml_training.tar.gz
                          | mlflow-artifacts/|  <- modelos, reports, .json
                          +------------------+
```

- VPC propia (no usa default), 1 subnet pública con IGW.
- 2 SGs estrictos: la EC2 de training **no expone** puertos públicos; el MLflow server expone 22 y 5000 al `admin_cidr`, y 5000 al SG de training.
- IAM: instance profile con acceso S3 (R/W solo al bucket) + CloudWatch logs. **No** se ponen credenciales en disco.
- S3: privado, versionado, encriptado AES256.
- EBS: gp3 cifrado.

## Pre-requisitos

```bash
brew install terraform awscli go-task   # macOS
# o el equivalente en tu OS
aws configure                            # credenciales con permiso de crear VPC/EC2/S3/IAM
```

## Despliegue

```bash
# 1) configurar
cp infra/terraform.tfvars.example infra/terraform.tfvars
$EDITOR infra/terraform.tfvars   # ajustar admin_cidr, s3_bucket_name (debe ser unico), etc.

# 2) validar y aplicar
task infra:init
task infra:plan
task infra:apply

# 3) ver outputs (URL de MLflow, IPs, comandos SSH listos para copiar)
task infra:output
```

Los outputs incluyen un bloque listo para pegar en tu `.env`.

## Destrucción

```bash
task infra:destroy
```

## Notas

- `terraform.tfstate` queda local por simplicidad. Para equipo, descomentar el bloque `backend "s3" {...}` en `versions.tf`.
- Las EC2 usan **AMI Ubuntu 22.04** (resuelta dinámicamente — siempre la última).
- Los `cloud-init/*.sh` son los `user_data`: idempotentes, loggean en `/var/log/cloud-init-output.log`.
- Cambiar `admin_cidr` a tu IP `/32` antes de exponer producción real (default `0.0.0.0/0` es **solo para test**).

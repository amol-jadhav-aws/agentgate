# AgentGate dev environment. One account, one region, USD 40/month ceiling.
# `make plan` / `make apply` / `make destroy`. prod is the same modules with prod.tfvars and the tag-based deploy.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
  # Uncomment after bootstrapping the state bucket and lock table (scripts in docs/runbook.md):
  # backend "s3" {
  #   bucket         = "agentgate-tfstate-<account-id>"
  #   key            = "dev/terraform.tfstate"
  #   region         = "ap-south-1"
  #   dynamodb_table = "agentgate-tflock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = local.tags
  }
}

locals {
  env  = "dev"
  name = "agentgate-${local.env}"
  tags = {
    Project     = "agentgate"
    Environment = local.env
    ManagedBy   = "terraform"
    Owner       = "amol"
    CostCenter  = "portfolio"
  }
}

data "aws_availability_zones" "available" { state = "available" }
data "aws_caller_identity" "current" {}

module "network" {
  source = "../../modules/network"
  name   = local.name
  azs    = slice(data.aws_availability_zones.available.names, 0, 2)
  tags   = local.tags
}

module "table" {
  source = "../../modules/dynamodb"
  name   = local.name
  tags   = local.tags
}

# -------------------------------------------------------------------- ECS cluster + ALB
resource "aws_ecs_cluster" "this" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "disabled" # cost: enable in prod
  }
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
}

resource "aws_security_group" "alb" {
  name   = "${local.name}-alb"
  vpc_id = module.network.vpc_id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "services" {
  name   = "${local.name}-services"
  vpc_id = module.network.vpc_id
  ingress {
    from_port       = 8080
    to_port         = 8081
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  ingress {
    from_port = 8080
    to_port   = 8081
    protocol  = "tcp"
    self      = true
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "this" {
  name               = local.name
  load_balancer_type = "application"
  subnets            = module.network.public_subnet_ids
  security_groups    = [aws_security_group.alb.id]
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "application/json"
      message_body = "{\"error\":\"not_found\"}"
      status_code  = "404"
    }
  }
}

module "redis" {
  source         = "../../modules/redis"
  name           = local.name
  vpc_id         = module.network.vpc_id
  subnet_ids     = module.network.private_subnet_ids
  allowed_sg_ids = [aws_security_group.services.id]
  tags           = local.tags
}

# -------------------------------------------------------------------- secrets
resource "aws_secretsmanager_secret" "keys" {
  name = "${local.name}/keys"
}

resource "random_password" "jwt" { length = 48 }
resource "random_password" "approval" { length = 48 }

resource "aws_secretsmanager_secret_version" "keys" {
  secret_id     = aws_secretsmanager_secret.keys.id
  secret_string = jsonencode({ jwt_secret = random_password.jwt.result, approval_signing_key = random_password.approval.result })
}

# -------------------------------------------------------------------- services
locals {
  common_env = {
    AGENTGATE_MODE                  = "aws"
    AGENTGATE_ENV_NAME              = local.env
    AGENTGATE_AWS_REGION            = var.region
    AGENTGATE_DYNAMODB_TABLE        = module.table.table_name
    AGENTGATE_REDIS_URL             = module.redis.endpoint
    AGENTGATE_CONFIG_DIR            = "/app/config"
    AGENTGATE_POLICY_DIR            = "/app/policies"
    AGENTGATE_GUARDRAIL_ID          = var.guardrail_id
    AGENTGATE_KB_ID                 = var.knowledge_base_id
    AGENTGATE_SECRETS_ID            = aws_secretsmanager_secret.keys.name
    OTEL_EXPORTER_OTLP_ENDPOINT     = "" # CloudWatch via ADOT sidecar in prod; empty = no exporter
  }
}

module "gateway" {
  source            = "../../modules/ecs_service"
  name              = "${local.name}-gateway"
  cluster_id        = aws_ecs_cluster.this.id
  image             = var.image
  command           = ["uvicorn", "agentgate.gateway.app:app", "--host", "0.0.0.0", "--port", "8080"]
  port              = 8080
  desired_count     = 1
  subnet_ids        = module.network.private_subnet_ids
  security_group_id = aws_security_group.services.id
  vpc_id            = module.network.vpc_id
  alb_listener_arn  = aws_lb_listener.http.arn
  path_pattern      = "/v1/chat*"
  environment       = local.common_env
  tags              = local.tags
  task_role_policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ApplyGuardrail"], Resource = "*" },
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:DeleteItem"], Resource = module.table.table_arn },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.keys.arn },
      { Effect = "Allow", Action = ["cloudwatch:PutMetricData", "xray:PutTraceSegments", "xray:PutTelemetryRecords"], Resource = "*" }
    ]
  })
}

module "toolgateway" {
  source            = "../../modules/ecs_service"
  name              = "${local.name}-toolgateway"
  cluster_id        = aws_ecs_cluster.this.id
  image             = var.image
  command           = ["uvicorn", "agentgate.toolgateway.app:app", "--host", "0.0.0.0", "--port", "8081"]
  port              = 8081
  desired_count     = 1
  subnet_ids        = module.network.private_subnet_ids
  security_group_id = aws_security_group.services.id
  vpc_id            = module.network.vpc_id
  alb_listener_arn  = aws_lb_listener.http.arn
  path_pattern      = "/v1/tools*"
  environment       = merge(local.common_env, { AGENTGATE_SLACK_WEBHOOK_URL = var.slack_webhook_url })
  tags              = local.tags
  task_role_policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # the ONLY infrastructure permission the tool gateway has: assume the per-tool roles
      { Effect = "Allow", Action = ["sts:AssumeRole", "sts:TagSession"], Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/agentgate-${local.env}-tool-*" },
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:DeleteItem"], Resource = module.table.table_arn },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.keys.arn },
      { Effect = "Allow", Action = ["cloudwatch:PutMetricData", "xray:PutTraceSegments", "xray:PutTelemetryRecords"], Resource = "*" }
    ]
  })
}

module "tool_roles" {
  source                    = "../../modules/iam_tool_roles"
  env                       = local.env
  toolgateway_task_role_arn = module.toolgateway.task_role_arn
  tags                      = local.tags
}

module "github_oidc" {
  source = "../../modules/github_oidc"
  repo   = var.github_repo
  ref    = "refs/heads/main"
  env    = local.env
  tags   = local.tags
}

# -------------------------------------------------------------------- triggers + budget guard
resource "aws_cloudwatch_event_rule" "alarm_to_agent" {
  name        = "${local.name}-alarm-to-ops-agent"
  description = "CloudWatch alarm state change -> Ops agent"
  event_pattern = jsonencode({
    source      = ["aws.cloudwatch"]
    detail-type = ["CloudWatch Alarm State Change"]
    detail      = { state = { value = ["ALARM"] }, alarmName = [{ prefix = "agentgate-demo-" }] }
  })
}

resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$agentgate"]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}

output "alb_dns" { value = aws_lb.this.dns_name }
output "table" { value = module.table.table_name }
output "deploy_role_arn" { value = module.github_oidc.role_arn }
output "tool_role_arns" { value = module.tool_roles.role_arns }

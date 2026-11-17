# One Fargate service behind the shared ALB. Used three times: gateway, toolgateway, agents.
variable "name" { type = string }
variable "cluster_id" { type = string }
variable "image" { type = string }
variable "command" { type = list(string) }
variable "port" { type = number }
variable "cpu" {
  type    = number
  default = 256
}
variable "memory" {
  type    = number
  default = 512
}
variable "desired_count" {
  type    = number
  default = 1
}
variable "subnet_ids" { type = list(string) }
variable "security_group_id" { type = string }
variable "environment" { type = map(string) }
variable "task_role_policy_json" { type = string }
variable "alb_listener_arn" {
  type    = string
  default = null
}
variable "vpc_id" { type = string }
variable "path_pattern" {
  type    = string
  default = null
}
variable "use_spot" {
  type    = bool
  default = true
}
variable "tags" { type = map(string) }

resource "aws_cloudwatch_log_group" "this" {
  name              = "/agentgate/${var.name}"
  retention_in_days = 14
  tags              = var.tags
}

data "aws_iam_policy_document" "task_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-exec"
  assume_role_policy = data.aws_iam_policy_document.task_trust.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.task_trust.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "task" {
  role   = aws_iam_role.task.id
  policy = var.task_role_policy_json
}

resource "aws_ecs_task_definition" "this" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }
  container_definitions = jsonencode([{
    name         = var.name
    image        = var.image
    command      = var.command
    essential    = true
    portMappings = [{ containerPort = var.port, protocol = "tcp" }]
    environment  = [for k, v in merge(var.environment, { PORT = tostring(var.port) }) : { name = k, value = v }]
    logConfiguration = {
      logDriver = "awslogs"
      options   = { awslogs-group = aws_cloudwatch_log_group.this.name, awslogs-region = data.aws_region.current.name, awslogs-stream-prefix = "ecs" }
    }
    healthCheck = { command = ["CMD-SHELL", "python -c \"import httpx;httpx.get('http://localhost:${var.port}/healthz').raise_for_status()\""], interval = 30, timeout = 5, retries = 3, startPeriod = 20 }
  }])
  tags = var.tags
}

resource "aws_lb_target_group" "this" {
  count       = var.alb_listener_arn == null ? 0 : 1
  name        = substr(replace(var.name, "_", "-"), 0, 32)
  port        = var.port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"
  health_check {
    path    = "/healthz"
    matcher = "200"
  }
  tags = var.tags
}

resource "aws_lb_listener_rule" "this" {
  count        = var.alb_listener_arn == null ? 0 : 1
  listener_arn = var.alb_listener_arn
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this[0].arn
  }
  condition {
    path_pattern { values = [var.path_pattern] }
  }
}

resource "aws_ecs_service" "this" {
  name            = var.name
  cluster         = var.cluster_id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count

  capacity_provider_strategy {
    capacity_provider = var.use_spot ? "FARGATE_SPOT" : "FARGATE"
    weight            = 1
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [var.security_group_id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = var.alb_listener_arn == null ? [] : [1]
    content {
      target_group_arn = aws_lb_target_group.this[0].arn
      container_name   = var.name
      container_port   = var.port
    }
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  propagate_tags = "SERVICE"
  tags           = var.tags
  depends_on     = [aws_lb_listener_rule.this]
}

data "aws_region" "current" {}

output "task_role_arn" { value = aws_iam_role.task.arn }
output "service_name" { value = aws_ecs_service.this.name }

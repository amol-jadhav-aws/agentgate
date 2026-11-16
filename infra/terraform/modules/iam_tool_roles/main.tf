# One IAM role per tool. The tool gateway's task role may assume each of them and nothing else;
# each role grants only the API calls that tool makes. The session name carries the run id.
variable "env" { type = string }
variable "toolgateway_task_role_arn" { type = string }
variable "tags" { type = map(string) }

locals {
  tools = {
    cw-describe-alarms = { actions = ["cloudwatch:DescribeAlarms"], resources = ["*"] }
    cw-get-metric      = { actions = ["cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData"], resources = ["*"] }
    ecs-describe       = { actions = ["ecs:DescribeServices", "ecs:DescribeTaskDefinition"], resources = ["*"] }
    ecs-scale          = { actions = ["ecs:UpdateService", "ecs:DescribeServices"], resources = ["arn:aws:ecs:*:*:service/demo/*"] }
    ecs-delete         = { actions = ["ecs:DeleteService", "ecs:DescribeServices"], resources = ["arn:aws:ecs:*:*:service/demo/*"] }
    ce-get-cost        = { actions = ["ce:GetCostAndUsage", "ce:GetAnomalies"], resources = ["*"] }
    kb-search          = { actions = ["bedrock:Retrieve"], resources = ["arn:aws:bedrock:*:*:knowledge-base/*"] }
  }
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "AWS"
      identifiers = [var.toolgateway_task_role_arn]
    }
    # the gateway must tag the session with the run id, so CloudTrail always carries it
    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/agentgate:run_id"
      values   = ["*"]
    }
  }
}

resource "aws_iam_role" "tool" {
  for_each             = local.tools
  name                 = "agentgate-${var.env}-tool-${each.key}"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
  tags                 = merge(var.tags, { "agentgate:tool" = each.key })
}

data "aws_iam_policy_document" "tool" {
  for_each = local.tools
  statement {
    actions   = each.value.actions
    resources = each.value.resources
  }
}

resource "aws_iam_role_policy" "tool" {
  for_each = local.tools
  role     = aws_iam_role.tool[each.key].id
  policy   = data.aws_iam_policy_document.tool[each.key].json
}

output "role_arns" { value = { for k, r in aws_iam_role.tool : k => r.arn } }

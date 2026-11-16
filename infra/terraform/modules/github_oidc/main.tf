# GitHub Actions -> AWS with OIDC. Trust is scoped to one repo and one branch (or tags for prod).
# There are no stored AWS access keys in GitHub.
variable "repo" {
  type        = string
  description = "owner/name"
}
variable "ref" {
  type        = string
  description = "e.g. refs/heads/main or refs/tags/v*"
}
variable "env" { type = string }
variable "tags" { type = map(string) }

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.repo}:ref:${var.ref}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name               = "agentgate-${var.env}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.trust.json
  tags               = var.tags
}

# Deploy needs: ECR push, ECS deploy, and Terraform's own resource management. Kept broad for a
# portfolio account; in a shared account split into a plan role and an apply role.
resource "aws_iam_role_policy_attachment" "deploy" {
  role       = aws_iam_role.deploy.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

resource "aws_iam_role_policy" "deploy_iam" {
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:PassRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:TagRole", "iam:UpdateAssumeRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy"]
      Resource = "arn:aws:iam::*:role/agentgate-*"
    }]
  })
}

output "role_arn" { value = aws_iam_role.deploy.arn }

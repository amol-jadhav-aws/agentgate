variable "region" {
  type    = string
  default = "ap-south-1"
}
variable "image" {
  type        = string
  description = "ECR image URI (set by the deploy workflow)"
  default     = "public.ecr.aws/docker/library/python:3.12-slim"
}
variable "github_repo" {
  type    = string
  default = "amol-jadhav-aws/agentgate"
}
variable "allowed_cidrs" {
  type        = list(string)
  description = "Who may reach the ALB. Your IP/32 for a portfolio deployment."
  default     = ["0.0.0.0/0"]
}
variable "monthly_budget_usd" {
  type    = number
  default = 40
}
variable "alert_email" {
  type    = string
  default = "amoljadhav.tech@gmail.com"
}
variable "guardrail_id" {
  type    = string
  default = ""
}
variable "knowledge_base_id" {
  type    = string
  default = ""
}
variable "slack_webhook_url" {
  type      = string
  default   = ""
  sensitive = true
}

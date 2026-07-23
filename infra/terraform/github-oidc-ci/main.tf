# GitHub Actions OIDC role for bounded AWS S3 integrity proofs (issue #13).
#
# This module does not create the test bucket (reuse the dedicated archive or a
# dedicated CI bucket). It only trusts GitHub OIDC and scopes object access to
# a single prefix so spend and blast radius stay bounded.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0"
    }
  }
}

variable "aws_region" {
  type    = string
  default = "eu-south-1"
}

variable "github_org" {
  type        = string
  description = "GitHub organization or user that owns the repository"
}

variable "github_repo" {
  type        = string
  description = "Repository name without org prefix"
}

variable "bucket_name" {
  type        = string
  description = "Existing versioned bucket used for CI integrity proofs"
}

variable "ci_prefix" {
  type        = string
  description = "Object key prefix the CI role may touch"
  default     = "ci/github"
}

variable "role_name" {
  type    = string
  default = "archive-github-oidc-ci"
}

provider "aws" {
  region = var.aws_region
}

data "tls_certificate" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github.certificates[0].sha1_fingerprint]
}

locals {
  policy_template = templatefile(
    "${path.module}/../archive-bucket/iam/github-oidc-ci-policy.json",
    {
      archive_ci_bucket_name = var.bucket_name
      archive_ci_prefix      = trimsuffix(var.ci_prefix, "/")
    }
  )
}

resource "aws_iam_role" "github_ci" {
  name = var.role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.github.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            "token.actions.githubusercontent.com:sub" = "repo:${var.github_org}/${var.github_repo}:*"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "github_ci_prefix" {
  name   = "archive-ci-prefix"
  role   = aws_iam_role.github_ci.id
  policy = local.policy_template
}

output "role_arn" {
  value = aws_iam_role.github_ci.arn
}

output "ci_prefix" {
  value = trimsuffix(var.ci_prefix, "/")
}

output "github_actions_vars" {
  value = {
    AWS_CI_ROLE_ARN     = aws_iam_role.github_ci.arn
    AWS_CI_TEST_BUCKET  = var.bucket_name
    AWS_CI_TEST_PREFIX  = trimsuffix(var.ci_prefix, "/")
    AWS_CI_REGION       = var.aws_region
  }
}

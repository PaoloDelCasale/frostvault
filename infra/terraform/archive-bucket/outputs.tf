output "bucket_name" {
  description = "Dedicated archive bucket name."
  value       = aws_s3_bucket.archive.bucket
}

output "bucket_arn" {
  description = "Dedicated archive bucket ARN."
  value       = aws_s3_bucket.archive.arn
}

output "aws_region" {
  description = "AWS region hosting the archive bucket."
  value       = var.aws_region
}

output "application_env" {
  description = "Environment variables required by FrostVault."
  value = {
    AWS_DEFAULT_REGION = var.aws_region
    S3_BUCKET          = aws_s3_bucket.archive.bucket
    VAULT_S3_BUCKET    = aws_s3_bucket.archive.bucket
  }
}

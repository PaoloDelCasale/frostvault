# Archive bucket Terraform module

Creates one dedicated, private, versioned S3 bucket for a FrostVault
installation.

## Baseline

- Versioning enabled
- Block Public Access (all four settings)
- Bucket owner enforced object ownership
- Default encryption SSE-S3 (`AES256`)
- Automatic abort of incomplete multipart uploads
- Object Lock **not** enabled

## Usage

```bash
cd infra/terraform/archive-bucket
cp terraform.tfvars.example terraform.tfvars
# Edit bucket_name and aws_region
terraform init
terraform plan
terraform apply
```

After `terraform apply`, copy the `application_env` output into the application
`.env` (`S3_BUCKET`, `VAULT_S3_BUCKET`, `AWS_DEFAULT_REGION`).

## IAM

See `iam/archive-operator-policy.json` for a least-privilege starting point that
covers upload, list, version read, restore, tagging, lifecycle management, and
the accepted single-identity deletion model documented in the project README.

Replace `${archive_bucket_name}` with your bucket name before attaching the
policy to an IAM user or role.

Attach `iam/frostvault-buckets-policy.json` as well, unchanged: it lets FrostVault
create, configure and verify its own buckets, starting with the System Bucket for
metadata backups (`frostvault-system-<random>`, see
[docs/metadata-backups.md](../../../docs/metadata-backups.md#system-bucket)). It
is scoped to `arn:aws:s3:::frostvault-*` and explicitly denies
`s3:BypassGovernanceRetention`.

## Teardown warning

`terraform destroy` removes the bucket only when it is empty. A versioned archive
bucket may retain delete markers and noncurrent versions. Review lifecycle and
recovery needs before destroying production buckets.

# Archive bucket Terraform module

FrostVault creates its own buckets: a Vault Bucket for every new Vault
(`frostvault-<vault name>-<8 random>`, ADR-0049, #735) and the System Bucket
for metadata backups. A new installation needs no bucket from this module, only
the IAM policy in `iam/frostvault-buckets-policy.json` (see [IAM](#iam)).

The module creates one shared, private, versioned S3 bucket: the bucket of an
installation from before Vault Buckets, whose older Vaults keep their objects
under `vaults/<uuid>/` in it. Keep it while such Vaults exist; FrostVault reads
each Vault's bucket from its Vault Target, never from configuration.

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

The `application_env` output is `AWS_DEFAULT_REGION`, the region of every new
Vault Bucket. `VAULT_S3_BUCKET` (and its alias `S3_BUCKET`) was removed in
#735 and is ignored.

## IAM

Attach `iam/frostvault-buckets-policy.json` unchanged to FrostVault's IAM user or
role. It is everything FrostVault needs in the buckets it creates, scoped to
`arn:aws:s3:::frostvault-*` and `arn:aws:s3:::frostvault-*/*`:

- create, configure and verify a bucket (Block Public Access, versioning,
  SSE-S3, `BucketOwnerEnforced`, lifecycle, tags, Object Lock configuration);
- upload, read, list versions, restore, tag and copy objects, and abort
  incomplete multipart uploads;
- delete object versions (the accepted single-identity deletion model in the
  [archive lifecycle guide](../../../docs/cloud-lifecycle.md): Cloud Deletion,
  Duplicate Cleanup, Vault Deletion).

- read the daily size metrics of the Vault Buckets with
  `cloudwatch:GetMetricStatistics` (#737). CloudWatch actions take no
  resource, so this one statement is on `"*"`; it grants no other CloudWatch
  action (`GetMetricData` is always billed and never used).

It explicitly denies `s3:BypassGovernanceRetention`. `s3:DeleteBucket` on
`frostvault-*` deletes an emptied Vault Bucket only when the Owner asked (#739).

`iam/archive-operator-policy.json` covers the same object operations on the
shared bucket of this module. Attach it only while older Vaults use that bucket,
after replacing `${archive_bucket_name}` with its name.

## Teardown warning

`terraform destroy` removes the bucket only when it is empty. A versioned archive
bucket may retain delete markers and noncurrent versions. Review lifecycle and
recovery needs before destroying production buckets.

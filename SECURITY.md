# Security Policy

## Supported versions

Security fixes are applied to the latest commit on `main`. Older revisions and
unreleased development branches are not supported.

## Reporting a vulnerability

Do not disclose suspected vulnerabilities in a public issue or discussion.
Use GitHub's **Report a vulnerability** flow in the repository Security tab
when it is available. If that flow is unavailable, contact the repository owner
through their GitHub profile and request a private channel before sharing
technical details.

Include the affected revision, a minimal reproduction, impact, and any known
mitigations. You should receive an acknowledgement within seven days. Please
allow time for validation and a coordinated fix before disclosure.

## Security expectations

- Never commit credentials, private keys, session tokens, or production data.
- Use short-lived AWS credentials and least-privilege IAM policies. The buckets
  FrostVault creates itself (a Vault Bucket for every new Vault, the System
  Bucket for metadata backups) need only
  `infra/terraform/archive-bucket/iam/frostvault-buckets-policy.json`, scoped to
  `arn:aws:s3:::frostvault-*` and `arn:aws:s3:::frostvault-*/*`, plus
  `cloudwatch:GetMetricStatistics` on `"*"` (CloudWatch actions take no resource;
  it only reads the daily size metrics of the Vault Buckets, never
  `cloudwatch:GetMetricData`, which is always billed); never grant
  `s3:BypassGovernanceRetention`, and keep `s3:DeleteBucket` out of it. A
  shared bucket from before Vault Buckets keeps
  `iam/archive-operator-policy.json`, scoped to that bucket.
- Keep security-sensitive changes isolated and covered by regression tests.
- Do not weaken CI security gates without a documented, time-bounded exception.

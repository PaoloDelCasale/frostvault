# Billed Cost comes from the CUR 2.0 export, sizes from GetMetricStatistics

Status: accepted by the Owner on 2026-11-17 (issue #732).

FrostVault shows a Vault's Billed Cost by reading, at most once a day, the newest
AWS Data Exports (CUR 2.0, CSV gzip, with resource IDs) file the Owner configures
once to land in the System Bucket. Line items are attributed to Vaults by bucket
name, so cost allocation tags are not required. Daily bytes and object counts
per storage class come from CloudWatch `GetMetricStatistics`.

## Why

The Cost Explorer API costs $0.01 per paginated request and CloudWatch
`GetMetricData` is always billed; the export is free apart from a few MB of S3
storage, and `GetMetricStatistics` stays inside CloudWatch's free API tier.
The export lags by up to a day, which is acceptable for a monthly cost view.
Without an export FrostVault shows only Cost Estimates and explains how to set
one up.

## Implementation notes (#738)

- The export is CUR 2.0, gzip CSV, with resource IDs, under `billing/cur/` in
  the System Bucket; setup steps, recommended columns, bucket policy and IAM in
  [docs/billed-cost.md](../billed-cost.md).
- Only the two newest `BILLING_PERIOD` folders are listed. The manifest schema
  is not documented by AWS and is parsed tolerantly; without a recognisable
  file list the period's data folder is listed instead. A month whose manifest
  (ETag, LastModified) is unchanged is not read again. Data files are streamed
  with the standard library's `gzip` and `csv`, never loaded whole.
- Line items count only when `line_item_resource_id` is a bucket FrostVault
  knows (name or ARN). The System Bucket and a shared bucket are installation
  rows, never a Vault's; a Vault in a shared bucket shows no figure, as for its
  CloudWatch sizes. Credits, refunds and discounts are a category of their own.
- A pass has its own short-timeout S3 client, a 5-minute budget and stops on
  shutdown or a connection error; a stopped pass records nothing.
- The last look is persisted (`billing_export_status`): the next one is a day
  later, or six hours while no export is there or after a transient error. A
  missing or unreadable export is a status with an error code, never a Problem
  or a notification.
- The Vault page shows the Billed Cost as the third block of its cloud usage
  (#737), next to the Cost Estimate.

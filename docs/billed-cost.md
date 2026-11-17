# Billed Cost from the AWS billing export

FrostVault shows each Vault's **Billed Cost**, what AWS actually charged for its
Vault Bucket, next to and separate from its **Cost Estimate** from list prices
([ADR-0051](adr/0051-billed-cost-from-the-billing-export.md), #738). The figures
come from an AWS Data Exports **CUR 2.0** export that the Owner sets up once, by
hand, to land in the [System Bucket](metadata-backups.md#system-bucket).
FrostVault never calls the Cost Explorer API (`ce:*`, billed $0.01 per request).

Without the export FrostVault shows only Cost Estimates and the steps below. A
missing export is not a Problem.

## Set up the export (once)

You need the System Bucket's name: the System page shows it (**System →
Billed cost**), and so does the metadata backups panel. In the AWS console, with
an identity allowed to manage billing exports:

1. Open **Billing and Cost Management → Data Exports** and choose **Create**.
2. **Export type:** *Standard data export*. **Export name:** for example
   `frostvault` (letters, digits, `-` and `_`).
3. **Data table:** *CUR 2.0*. Tick **Include resource IDs**: FrostVault
   recognises each Vault Bucket by `line_item_resource_id`, so cost allocation
   tags are not needed. **Time granularity:** *Daily*. Leave split cost
   allocation and the other options off.
4. **Column selection:** all columns, or at least `line_item_resource_id`,
   `line_item_unblended_cost`, `line_item_currency_code`,
   `line_item_usage_type`, `line_item_operation`, `line_item_product_code` and
   `line_item_usage_end_date`, with their default names (no SQL renaming).
5. **Data export refresh cadence:** *Daily* (the only choice). **File
   versioning:** *Overwrite existing data export file*. **Report data
   integration:** none. **Compression type and file format:** *gzip – text/csv*
   (not Parquet).
6. **Data export storage settings:** *This account* → **Configure** → *Select
   existing bucket* → the System Bucket (`frostvault-system-…`, or your
   `METADATA_BACKUP_BUCKET`). Review the bucket policy AWS proposes (below) and
   confirm it.
7. **S3 path prefix:** `billing/cur`.
8. Choose **Create**. AWS delivers the first export within 24 hours and then
   refreshes it at least once a day.

The export lands at
`s3://<System Bucket>/billing/cur/<export name>/data/BILLING_PERIOD=YYYY-MM/<export name>-00001.csv.gz`,
with its `Manifest.json` under `…/<export name>/metadata/BILLING_PERIOD=YYYY-MM/`.
Use one export under `billing/cur/`; if there are several, FrostVault reads the
one delivered last.

### The bucket policy AWS needs

Data Exports writes into the bucket only after the bucket policy lets its
service principal do so. The console adds this statement (from the
[AWS Data Exports guide](https://docs.aws.amazon.com/cur/latest/userguide/dataexports-s3-bucket.html);
`ACCOUNT_ID` is your account, `BUCKET` the System Bucket):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EnableAWSDataExportsToWriteToS3",
      "Effect": "Allow",
      "Principal": { "Service": ["bcm-data-exports.amazonaws.com"] },
      "Action": ["s3:PutObject"],
      "Resource": "arn:aws:s3:::BUCKET/*",
      "Condition": {
        "ArnLike": { "aws:SourceArn": "arn:aws:bcm-data-exports:us-east-1:ACCOUNT_ID:export/*" },
        "StringEquals": { "aws:SourceAccount": "ACCOUNT_ID" }
      }
    }
  ]
}
```

The console may write a broader variant that also names
`billingreports.amazonaws.com` (legacy CUR) and `s3:GetBucketPolicy`; either
works. The console **replaces** an existing bucket policy: a System Bucket
FrostVault created has none. If you set `METADATA_BACKUP_BUCKET` to a bucket
with its own policy, add the statement to it instead.

FrostVault's System Bucket checks keep passing with this policy: they cover
Block Public Access (a policy for an AWS service principal with source
conditions is not public), versioning, SSE-S3, Object Ownership
`BucketOwnerEnforced`, FrostVault's abort rule and the absence of Object Lock,
never the bucket policy. Data Exports delivers to buckets with ACLs disabled.

### IAM for FrostVault

FrostVault only lists and reads the prefix: `s3:ListBucket` (on the bucket, with
prefix `billing/cur/`) and `s3:GetObject` (on `billing/cur/*`). Both are already
in [`frostvault-buckets-policy.json`](../infra/terraform/archive-bucket/iam/frostvault-buckets-policy.json)
for a FrostVault-created `frostvault-system-*` bucket, so nothing changes. For an
operator-supplied `METADATA_BACKUP_BUCKET` outside `frostvault-*`, grant them on
that bucket in addition to the
[backup permissions](metadata-backups.md#iam-permissions). No `ce:*`, `cur:*`
or `bcm-data-exports:*` permission is needed or used.

### Storage

The export is free apart from a few MB of S3 storage. The System Bucket is
versioned: with *Overwrite*, every daily refresh leaves the previous file as a
noncurrent version. Add a lifecycle rule to the System Bucket (Owner-added
rules are allowed) that expires noncurrent versions under `billing/` after, for
example, 7 days. Metadata backups live under `system/backups/` and are never
touched by the export.

## How FrostVault reads it

- **When:** the background loop asks every 15 minutes whether a look is due; the
  time of the last look is stored in the catalog (`billing_export_status`), so a
  restart never reads early. After a read (or an error) the next look is a day
  later; while no export is there, every six hours (a few list requests), so
  the first delivery is found sooner.
- **What:** it lists `billing/cur/<export>/metadata/BILLING_PERIOD=…/`, takes
  the newest `<export>-Manifest.json` (the one AWS overwrites with every
  refresh, also in *create new* mode) and the one of the month before, when
  present, and reads the gzip CSV files each manifest lists. Files are streamed
  with Python's `gzip` and `csv` modules, one row at a time: memory does not
  grow with the size of the export. Only files under that export's
  `data/BILLING_PERIOD=…/` folder in the same bucket are read.
- **Attribution:** a line item counts when its `line_item_resource_id` is the
  name or the `arn:aws:s3:::` ARN of a bucket FrostVault knows. Every other line
  of the account (other buckets, other services, taxes, credits without a
  resource) is skipped and never stored.
  - a **Vault Bucket**: the Vault's Billed Cost;
  - the **System Bucket**: an installation row;
  - the **shared bucket** of older Vaults: an installation row. A Vault in it
    shows the whole bucket's amounts, marked as shared with other Vaults.
- **Categories**, from `line_item_usage_type` and `line_item_operation`:
  transitions/lifecycle (operation names a transition, `S3-GDATransition`),
  retrievals and restores (`Restore…`, `…Retrieval…`), storage (`TimedStorage`,
  and `EarlyDelete`: the remainder of a Minimum Storage Duration), data transfer
  (`DataTransfer`, `…-Out-Bytes`), requests (`Requests-…`), other.
- **Stored:** per billing period (`BILLING_PERIOD`, current and previous month),
  bucket and category, the sum of `line_item_unblended_cost` in the billing
  currency (`line_item_currency_code`), with the time AWS delivered the manifest
  and the newest `line_item_usage_end_date` in it. A read replaces every stored
  total at once.

## What the Owner sees

- **Vault page → Settings → Billed cost:** per month the Billed Cost, by
  category, with the date of the data (AWS lags 24–48 hours and may still
  adjust a month in the two weeks after it ends), and apart from it this
  month's Cost Estimate. Without a readable export: the estimate and a link to
  the setup steps.
- **System → Billed cost:** the state and, once read, every Vault's and
  installation row's Billed Cost per month, plus the setup steps.
- **Overview:** under the estimated cost, the newest month's Billed Cost, or a
  link to the setup.

States: *no System Bucket* (nothing to read), *checking*, *not configured*
(nothing under `billing/cur/`), *waiting for the first export* (the export's
folder exists without a manifest yet), *OK*, *error*. An error (for example
`S3 refused s3:GetObject (AccessDenied)`, a Parquet export, a missing
`line_item_resource_id` column) is shown only there: it opens no Problem and
sends no notification, and FrostVault tries again the next day. While it lasts,
only the Cost Estimate is shown. Provider messages are never stored or shown,
only error codes.

## Upgrading

Migration `0012_billed_cost` adds `billing_export_status` (one row) and
`billed_costs`; it makes no AWS call and backfills nothing.

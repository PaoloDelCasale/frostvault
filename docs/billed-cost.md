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
4. **Column selection:** select only the columns FrostVault reads, with their
   default names (no SQL renaming): `line_item_resource_id`,
   `line_item_unblended_cost`, `line_item_net_unblended_cost`,
   `line_item_currency_code`, `line_item_usage_type`, `line_item_operation`,
   `line_item_product_code`, `line_item_usage_end_date` and
   `line_item_line_item_type`. All columns work too, but make every daily file
   much larger (the resource tag and product columns are long JSON maps), and
   so every read slower. The first five are required; without the others
   FrostVault uses the unblended cost, files credits by usage type and shows no
   data date.
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
with its manifest (`<export name>-Manifest.json`) under
`…/<export name>/metadata/BILLING_PERIOD=YYYY-MM/`. With *Create new data
export file* the files are in a `<timestamp>-<execution-id>/` folder of their
own per refresh; FrostVault reads only the newest one, but *Overwrite* keeps
the bucket smaller. Use one export under `billing/cur/`; if there are several,
FrostVault reads the one delivered last.

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
works. The console **replaces** an existing bucket policy instead of adding
the statement to it: a System Bucket FrostVault created has none. If you set
`METADATA_BACKUP_BUCKET` to a bucket with its own policy, add the statement to
that policy by hand instead of letting the console write it. The setup steps on
the System page say the same.

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
  restart never reads early. After a read the next look is a day later; while
  no export is there, every six hours (a few list requests), so the first
  delivery is found sooner. After an error see [Errors](#errors).
- **What:** it lists `billing/cur/<export>/metadata/`, keeps the two newest
  `BILLING_PERIOD=…/` folders and in each takes the newest object whose name
  ends in `Manifest.json` directly in it (`<export>-Manifest.json` or
  `Manifest.json`; in *create new* mode without one there, the manifest of the
  newest `<timestamp>-<execution-id>/` folder). The newest month and the one
  before it are read.
- **The manifest:** AWS does not document the schema of the Data Exports
  manifest, so FrostVault parses it tolerantly. A list of data files under
  `dataFiles` (or `dataFileS3Keys`, `reportKeys`, `files`), as keys, `s3://`
  URIs or objects naming one, is used. When there is no recognisable list,
  FrostVault lists `data/BILLING_PERIOD=…/` instead and reads its `*.csv.gz`
  files (in *create new* mode, only those of the newest
  `<timestamp>-<execution-id>/` folder). Files in another bucket or outside the
  period's data folder are refused, and so is Parquet.
- **Only what changed:** for each month FrostVault stores the manifest's key,
  ETag and LastModified (`billed_cost_periods`). A month whose manifest is
  unchanged since the last read, with the same known buckets, keeps its totals
  and its data files are not read again: usually only the current month is
  read each day.
- **Streaming:** files are read with Python's `gzip` and `csv` modules, one row
  at a time: memory does not grow with the size of the export.
- **Bounded:** a pass uses its own S3 client (3 s to connect, 10 s per read,
  two attempts) and stops at once on a connection or read timeout. It runs at
  most 5 minutes, checked between files and every 5,000 rows, and stops on
  shutdown. A stopped or over-budget pass records nothing: the previous
  totals and state stay. After a shutdown the look is due again at once; over
  budget, at the usual interval.
- **Attribution:** a line item counts when its `line_item_resource_id` is the
  name or the `arn:aws:s3:::` ARN of a bucket FrostVault knows. Every other line
  of the account (other buckets, other services, taxes, credits without a
  resource) is skipped and never stored. The currency and the data date come
  from every line.
  - a **Vault Bucket**: the Vault's Billed Cost;
  - the **System Bucket**: an installation row;
  - the **shared bucket** of older Vaults: an installation row. A Vault in it
    has no Billed Cost of its own: AWS bills the whole bucket.
- **Amounts:** `line_item_net_unblended_cost` (after discounts) when the export
  has the column and the cell is not empty, else `line_item_unblended_cost`, in
  the billing currency (`line_item_currency_code`).
- **Categories:** line items whose `line_item_line_item_type` is `Credit`,
  `Refund` or a `…Discount` are **credits and discounts**, shown apart. The
  others by `line_item_usage_type` and `line_item_operation`:
  transitions/lifecycle (operation names a transition, `S3-GDATransition`),
  retrievals and restores (`Restore…`, `…Retrieval…`), storage (`TimedStorage`,
  and `EarlyDelete`: the remainder of a Minimum Storage Duration), data transfer
  (`DataTransfer`, `…-Out-Bytes`), requests (`Requests-…`), other.
- **Stored:** per billing period, bucket and category, the sum of the amounts,
  with the time AWS delivered the newest manifest and the newest
  `line_item_usage_end_date`.

### Errors

An error is stored as a code (translated by the UI) with at most an S3 action
and AWS error code, column or currency names: provider messages are never
stored or shown. It opens no Problem and sends no notification; while it lasts
only the state is shown, and the totals of the last good read stay stored.

- **Transient**, retried after 6 hours: S3 unreachable or timed out, throttled,
  a 5xx answer.
- **Deterministic**, retried after a day: no permission (`AccessDenied`), other
  S3 refusals, no AWS credentials, a Parquet export, a missing column (for
  example `line_item_resource_id` without *Include resource IDs*), an
  unreadable file or manifest, a manifest that names files elsewhere.

### Not up to date

The figures are marked **data not up to date** when the newest export AWS
delivered is more than 3 days old, or after the 3rd of a month the current
month is still missing from it. Check the export in the AWS Billing console.

## What the Owner sees

- **Vault page → Settings → Cloud storage and cost:** the third block, *Billed
  by AWS*, next to the catalog estimate and the CloudWatch sizes: per month the
  Billed Cost by category, credits and discounts included, with the date of the
  data (AWS lags 24–48 hours and may still adjust a month in the two weeks
  after it ends). Without a readable export: the state and a link to the System
  page. A Vault in a shared bucket shows only a note pointing to the
  installation rows of the System page, like its CloudWatch block.
- **System → Billed cost:** the state and, once read, every Vault's and
  installation row's Billed Cost per month, plus the setup steps.
- **Overview:** under the estimated cost, the newest month's Billed Cost of the
  Vault Buckets only («FrostVault buckets»), never the whole AWS bill. The setup
  steps are only on the System page.

States: *no System Bucket* (nothing to read), *checking*, *not configured*
(nothing under `billing/cur/`), *waiting for the first export* (the export's
folder exists without a manifest yet), *OK*, *error*.

## Upgrading

Migration `0012_billed_cost` (after `0011_bucket_metrics`) adds
`billing_export_status` (one row), `billed_cost_periods` (the manifest each
month was read from) and `billed_costs`; it makes no AWS call and backfills
nothing.

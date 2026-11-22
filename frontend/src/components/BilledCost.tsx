/* eslint-disable react-refresh/only-export-components -- shared Billed Cost helpers live with their components. */
import type { BillingExportStatus, VaultBilledCost } from "@/api";
import { useI18n } from "@/i18n";
import { formatDate, formatDateTime, formatMoney } from "@/pages/archive/format";

/** The categories of a Billed Cost, in the order the UI lists them (#738). */
const BILLED_CATEGORIES = ["storage", "requests", "transitions", "retrievals", "data_transfer", "other", "credits"] as const;
type BilledCategories = Record<(typeof BILLED_CATEGORIES)[number], number>;
type ExportStatus = Pick<BillingExportStatus, "state" | "bucket" | "prefix" | "checked_at" | "next_check_at" | "exported_at" | "data_through" | "error_code" | "error_detail">;

/** An AWS bill amount: exact, never «~», in the billing currency. */
export function billedMoney(value: number, currency: string | null | undefined, locale?: string): string {
  return formatMoney(value, { currency: currency ?? "USD", locale });
}

/** «Data up to 16 Nov 2026 (delivered by AWS 17 Nov 2026, 06:12)»: the export lags 24–48 hours. */
export function BilledDataDate({ status }: { status: Pick<VaultBilledCost, "data_through" | "exported_at"> }) {
  const { t, locale } = useI18n();
  return <p className="text-xs text-muted" data-testid="billed-cost-date">
    {status.data_through
      ? t("billed_cost.data_through", { date: formatDate(status.data_through, locale), exported: formatDateTime(status.exported_at, locale) })
      : t("billed_cost.exported_at", { exported: formatDateTime(status.exported_at, locale) })}
  </p>;
}

/** «Data not up to date»: the newest export is over 3 days old, or the current month is missing. */
export function BilledStale({ status }: { status: Pick<VaultBilledCost, "stale" | "exported_at"> }) {
  const { t, locale } = useI18n();
  if (!status.stale) return null;
  return <p role="note" className="text-sm font-semibold" data-testid="billed-cost-stale">
    {t("billed_cost.stale", { exported: formatDateTime(status.exported_at, locale) })}
  </p>;
}

/** The non-zero categories of one period, as a compact list. */
export function BilledCategoryList({ categories, currency }: { categories: BilledCategories; currency: string | null | undefined }) {
  const { t, locale } = useI18n();
  const shown = BILLED_CATEGORIES.filter((name) => categories[name] !== 0);
  if (shown.length === 0) return <p className="text-sm text-muted">{t("billed_cost.no_charges")}</p>;
  return <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-0.5 text-sm">
    {shown.map((name) => <div key={name} className="contents" data-testid={`billed-category-${name}`}>
      <dt className="text-muted">{t(`billed_cost.category_${name}`)}</dt>
      <dd className="text-right tabular-nums">{billedMoney(categories[name], currency, locale)}</dd>
    </div>)}
  </dl>;
}

/** What the billing export's state means, in one line; nothing when it was read. */
export function BillingExportState({ status }: { status: ExportStatus }) {
  const { t, locale } = useI18n();
  if (status.state === "ok") return null;
  const location = `s3://${status.bucket ?? "…"}/${status.prefix}`;
  const next = status.next_check_at ? formatDateTime(status.next_check_at, locale) : "—";
  // The backend stores an error code, never a provider message: translate it here.
  const error = status.error_code ? t(`billed_cost.error.${status.error_code}`, { detail: status.error_detail ?? "" }) : "";
  return <p role="status" className="text-sm break-words" data-testid="billing-export-state" data-state={status.state} data-error={status.error_code ?? undefined}>
    {t(`billed_cost.state_${status.state}`, { location, next, error })}
  </p>;
}

/** The bucket policy AWS Data Exports needs to deliver into the System Bucket. */
function billingBucketPolicy(bucket: string): string {
  return JSON.stringify({
    Version: "2012-10-17",
    Statement: [{
      Sid: "EnableAWSDataExportsToWriteToS3",
      Effect: "Allow",
      Principal: { Service: ["bcm-data-exports.amazonaws.com"] },
      Action: ["s3:PutObject"],
      Resource: `arn:aws:s3:::${bucket}/*`,
      Condition: {
        ArnLike: { "aws:SourceArn": "arn:aws:bcm-data-exports:us-east-1:ACCOUNT_ID:export/*" },
        StringEquals: { "aws:SourceAccount": "ACCOUNT_ID" },
      },
    }],
  }, null, 2);
}

/** The one-time steps in the AWS Billing console (docs/billed-cost.md). */
export function BillingSetupSteps({ bucket, prefix }: { bucket: string | null; prefix: string }) {
  const { t } = useI18n();
  const name = bucket ?? t("billed_cost.setup_bucket_unknown");
  const folder = (prefix || "billing/cur/").replace(/\/+$/, "");
  return <div className="grid min-w-0 gap-2 text-sm break-words" data-testid="billing-setup">
    <ol className="grid list-decimal gap-1 pl-5">
      {(["open", "type", "table", "columns", "format", "bucket", "prefix", "create"] as const).map((step) =>
        <li key={step}>{t(`billed_cost.setup_${step}`, { bucket: name, prefix: folder })}</li>)}
    </ol>
    <p className="text-muted">{t("billed_cost.setup_policy")}</p>
    <details className="text-muted"><summary className="min-h-11 w-fit cursor-pointer py-2 font-semibold text-ink">{t("billed_cost.setup_policy_show")}</summary>
      <pre className="overflow-x-auto rounded-card border border-line bg-canvas p-3 text-xs" data-testid="billing-policy">{billingBucketPolicy(name)}</pre>
    </details>
    <p className="text-muted">{t("billed_cost.setup_cost")}</p>
  </div>;
}

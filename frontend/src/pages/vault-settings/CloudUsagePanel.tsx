import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiQueryKeys, vaultApi, type VaultCloudUsage } from "@/api";
import { BilledCategoryList, BilledDataDate, BilledStale, BillingExportState, billedMoney } from "@/components/BilledCost";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/lib/errorMessage";
import { formatApproxMoney, formatBytes, formatDate, formatDateTime, formatGroupedCount, formatMonth } from "@/lib/format";
import { systemUrl } from "@/router";

type Translate = ReturnType<typeof useI18n>["t"];
type Estimate = VaultCloudUsage["estimate"];
type Measured = VaultCloudUsage["measured"];

/** One source of figures about the Vault's cloud usage, side by side with the others. */
export function CloudUsageBlock({ id, title, help, children }: { id: string; title: string; help: string; children: ReactNode }) {
  return <div className="grid min-w-0 content-start gap-2 rounded-card border border-line p-3" data-testid={`cloud-usage-${id}`}>
    <div className="grid gap-0.5">
      <h3 className="font-semibold">{title}</h3>
      <p className="text-xs text-muted">{help}</p>
    </div>
    {children}
  </div>;
}

function EstimateBlock({ estimate }: { estimate: Estimate }) {
  const { t, locale } = useI18n();
  const classes = estimate.by_storage_class ?? [];
  const count = (value: number) => formatGroupedCount(value, locale);
  const money = (value: number) => formatApproxMoney(value, { currency: estimate.currency, locale });
  return <CloudUsageBlock id="estimate" title={t("cloud_usage.estimate_title")} help={t("cloud_usage.estimate_help")}>
    {classes.length === 0 ? <p className="text-sm" data-testid="cloud-usage-estimate-none">{t("cloud_usage.estimate_none")}</p>
      : <div className="overflow-x-auto"><table className="w-full text-sm">
        <thead className="text-left text-xs text-muted"><tr>
          <th scope="col" className="py-1 pr-2">{t("cloud_usage.col_class")}</th>
          <th scope="col" className="py-1 pr-2 text-right">{t("cloud_usage.col_versions")}</th>
          <th scope="col" className="py-1 pr-2 text-right">{t("cloud_usage.col_size")}</th>
          <th scope="col" className="py-1 text-right">{t("cloud_usage.col_monthly")}</th>
        </tr></thead>
        <tbody>{classes.map((entry) => <tr key={entry.storage_class} className="border-t border-line" data-testid={`cloud-usage-estimate-${entry.storage_class}`}>
          <td className="py-1 pr-2">{t(`storage.${entry.storage_class}`)}</td>
          <td className="py-1 pr-2 text-right tabular-nums">{count(entry.versions)}</td>
          <td className="py-1 pr-2 text-right tabular-nums">{formatBytes(entry.bytes, locale)}</td>
          <td className="py-1 text-right tabular-nums">{money(entry.monthly_cost)}</td>
        </tr>)}</tbody>
        {classes.length > 1 && <tfoot><tr className="border-t border-line font-semibold" data-testid="cloud-usage-estimate-total">
          <th scope="row" className="py-1 pr-2 text-left">{t("cloud_usage.total")}</th>
          <td className="py-1 pr-2 text-right tabular-nums">{count(estimate.versions)}</td>
          <td className="py-1 pr-2 text-right tabular-nums">{formatBytes(estimate.bytes, locale)}</td>
          <td className="py-1 text-right tabular-nums">{money(estimate.monthly_cost)}</td>
        </tr></tfoot>}
      </table></div>}
    <p className="text-xs text-muted" data-testid="cloud-usage-estimate-disclaimer">{t("cloud_usage.estimate_disclaimer", { region: estimate.region, date: formatDate(estimate.pricing_effective_at, locale) })}</p>
  </CloudUsageBlock>;
}

/** Why no (or no fresh) CloudWatch figure is shown: a note, never a Problem. */
function measuredNote(measured: Measured, t: Translate): string | null {
  const outcome = measured.status === "available" ? measured.last_read?.outcome : measured.status;
  const code = measured.last_read?.error_code ?? "";
  switch (outcome) {
    case "shared_bucket": return t("cloud_usage.shared_bucket");
    case "provisioning": return t("cloud_usage.provisioning");
    case "pending": return t("cloud_usage.pending");
    case "no_data": return measured.status === "available" ? null : t("cloud_usage.no_data");
    case "access_denied": return t("cloud_usage.access_denied");
    case "failed": return t("cloud_usage.failed", { code });
    default: return null;
  }
}

function MeasuredBlock({ measured }: { measured: Measured }) {
  const { t, locale } = useI18n();
  const note = measuredNote(measured, t);
  const available = measured.status === "available";
  const stale = available && note !== null;
  const classes = measured.by_storage_class ?? [];
  const history = measured.history ?? [];
  const objects = measured.object_count;
  const next = measured.status !== "shared_bucket" && measured.status !== "provisioning" ? measured.next_read_after : null;
  return <CloudUsageBlock id="measured" title={t("cloud_usage.measured_title")} help={t("cloud_usage.measured_help")}>
    {note && <p className="text-sm" role="note" data-testid="cloud-usage-measured-note">{note}{stale && <> {t("cloud_usage.stale")}</>}</p>}
    {available && <>
      <p className="text-sm font-semibold" data-testid="cloud-usage-measured-day">{t("cloud_usage.measured_day", { date: formatDate(measured.day, locale), read_at: formatDateTime(measured.read_at, locale) })}</p>
      {classes.length === 0 ? <p className="text-sm">{t("cloud_usage.measured_none")}</p>
        : <div className="overflow-x-auto"><table className="w-full text-sm">
          <thead className="text-left text-xs text-muted"><tr>
            <th scope="col" className="py-1 pr-2">{t("cloud_usage.col_class")}</th>
            <th scope="col" className="py-1 pr-2 text-right">{t("cloud_usage.col_size")}</th>
            <th scope="col" className="py-1 text-right">{t("cloud_usage.col_overhead")}</th>
          </tr></thead>
          <tbody>{classes.map((entry) => <tr key={entry.storage_class} className="border-t border-line align-top" data-testid={`cloud-usage-measured-${entry.storage_class}`}>
            <td className="py-1 pr-2">{t(`storage.${entry.storage_class}`)}</td>
            <td className="py-1 pr-2 text-right tabular-nums">{formatBytes(entry.bytes, locale)}</td>
            <td className="py-1 text-right tabular-nums">{entry.overhead_bytes > 0 ? <>
              {formatBytes(entry.overhead_bytes, locale)}
              <ul className="text-xs text-muted">{entry.overhead.map((part) => <li key={part.storage_type}>
                {formatBytes(part.bytes, locale)} {t(`cloud_usage.kind.${part.kind}`)}{part.billed_as !== entry.storage_class && <> · {t("cloud_usage.billed_as", { class: t(`storage.${part.billed_as}`) })}</>}
              </li>)}</ul>
            </> : "—"}</td>
          </tr>)}</tbody>
          {classes.length > 1 && <tfoot><tr className="border-t border-line font-semibold" data-testid="cloud-usage-measured-total">
            <th scope="row" className="py-1 pr-2 text-left">{t("cloud_usage.total")}</th>
            <td className="py-1 pr-2 text-right tabular-nums">{formatBytes(measured.bytes ?? 0, locale)}</td>
            <td className="py-1 text-right tabular-nums">{formatBytes(measured.overhead_bytes ?? 0, locale)}</td>
          </tr></tfoot>}
        </table></div>}
      <p className="text-sm" data-testid="cloud-usage-measured-objects">{objects === null || objects === undefined
        ? t("cloud_usage.measured_objects_unknown")
        : t("cloud_usage.measured_objects", { count: objects, count_shown: formatGroupedCount(objects, locale) })}</p>
    </>}
    {measured.status !== "shared_bucket" && <p className="text-xs text-muted" data-testid="cloud-usage-measured-lag">
      {t("cloud_usage.measured_lag")}{next && <> {t("cloud_usage.next_read", { date: formatDateTime(next, locale) })}</>}
    </p>}
    {available && <details className="text-xs text-muted">
      <summary className="min-h-11 w-fit cursor-pointer py-2 font-semibold text-ink">{t("cloud_usage.overhead_title")}</summary>
      <div className="grid gap-1" data-testid="cloud-usage-overhead-help">
        <p>{t("cloud_usage.overhead_help")}</p>
        <p>{t("cloud_usage.overhead_object")}</p>
        <p>{t("cloud_usage.overhead_size")}</p>
        <p>{t("cloud_usage.overhead_staging")}</p>
        <p>{t("cloud_usage.measured_objects_help")}</p>
      </div>
    </details>}
    {history.length > 1 && <details className="text-sm">
      <summary className="min-h-11 w-fit cursor-pointer py-2 font-semibold">{t("cloud_usage.history_title", { count: history.length, count_shown: formatGroupedCount(history.length, locale) })}</summary>
      <div className="overflow-x-auto"><table className="w-full" data-testid="cloud-usage-history">
        <thead className="text-left text-xs text-muted"><tr>
          <th scope="col" className="py-1 pr-2">{t("cloud_usage.col_day")}</th>
          <th scope="col" className="py-1 pr-2 text-right">{t("cloud_usage.col_size")}</th>
          <th scope="col" className="py-1 pr-2 text-right">{t("cloud_usage.col_overhead")}</th>
          <th scope="col" className="py-1 text-right">{t("cloud_usage.col_objects")}</th>
        </tr></thead>
        <tbody>{history.map((day) => <tr key={day.day} className="border-t border-line">
          <td className="py-1 pr-2">{formatDate(day.day, locale)}</td>
          <td className="py-1 pr-2 text-right tabular-nums">{formatBytes(day.bytes, locale)}</td>
          <td className="py-1 pr-2 text-right tabular-nums">{formatBytes(day.overhead_bytes, locale)}</td>
          <td className="py-1 text-right tabular-nums">{day.object_count === null || day.object_count === undefined ? "—" : formatGroupedCount(day.object_count, locale)}</td>
        </tr>)}</tbody>
      </table></div>
    </details>}
  </CloudUsageBlock>;
}

/**
 * What AWS billed for the Vault's bucket, from the billing export (#738).
 * Exact amounts in the billing currency, never «~». A Vault on a shared
 * bucket has no figure of its own: a note points to the installation rows of
 * the System page. Without a readable export, its state and the way to the
 * setup steps on the System page.
 */
function BilledCostBlock({ vaultId }: { vaultId: number }) {
  const { t, locale } = useI18n();
  const billed = useQuery({ queryKey: apiQueryKeys.billedCost(vaultId), queryFn: () => vaultApi(vaultId).billedCost(), retry: false });
  const data = billed.data;
  const link = (key: string) => <a className="w-fit text-sm font-semibold text-ink underline" href={systemUrl("billing-export")} data-testid="billed-cost-setup-link">{t(key)}</a>;
  return <CloudUsageBlock id="billed" title={t("billed_cost.billed_title")} help={t("billed_cost.billed_help")}>
    {billed.error ? <p role="alert" className="text-sm text-destructive">{errorMessage(billed.error, t)}</p>
      : !data ? <p role="status" className="text-sm text-muted">{t("ui.loading")}</p>
        : data.shared ? <>
          <p className="text-sm" role="note" data-testid="billed-cost-shared">{t("billed_cost.shared_bucket")}</p>
          {link("billed_cost.installation_link")}
        </> : data.state === "ok" ? <>
          <BilledStale status={data} />
          {data.periods.length === 0 ? <p className="text-sm text-muted">{t("billed_cost.no_charges")}</p>
            : data.periods.map((period) => <div key={period.period} className="grid gap-1 border-t border-line pt-2 text-sm first-of-type:border-t-0 first-of-type:pt-0" data-testid={`billed-cost-${period.period}`}>
              <p className="flex flex-wrap justify-between gap-2"><span className="font-semibold">{formatMonth(period.period, locale)}</span>
                <span className="font-semibold tabular-nums" data-testid="billed-cost-total">{billedMoney(period.total, data.currency, locale)}</span></p>
              <BilledCategoryList categories={period.categories} currency={data.currency} />
            </div>)}
          <BilledDataDate status={data} />
          <p className="text-xs text-muted" data-testid="billed-cost-lag">{t("billed_cost.lag")}</p>
        </> : <>
          <BillingExportState status={data} />
          {data.state !== "unavailable" && data.state !== "checking"
            && link(data.state === "error" ? "billed_cost.status_link" : "billed_cost.setup_link")}
        </>}
  </CloudUsageBlock>;
}

/**
 * The Vault's cloud usage (#737, #738): the catalog's Cost Estimate, what
 * CloudWatch measured in its Vault Bucket and what AWS billed for it, each
 * its own block side by side. Reads only the catalog; never AWS.
 */
export function CloudUsagePanel({ vaultId }: { vaultId: number }) {
  const { t } = useI18n();
  const usage = useQuery({ queryKey: apiQueryKeys.cloudUsage(vaultId), queryFn: () => vaultApi(vaultId).cloudUsage(), retry: false });
  if (usage.error) return <p role="alert" className="text-sm text-destructive">{errorMessage(usage.error, t)}</p>;
  if (!usage.data?.estimate || !usage.data.measured) return <p role="status" className="text-sm text-muted">{t("ui.loading")}</p>;
  return <div className="grid min-w-0 gap-3 lg:grid-cols-2 xl:grid-cols-3" data-testid="cloud-usage">
    <EstimateBlock estimate={usage.data.estimate} />
    <MeasuredBlock measured={usage.data.measured} />
    <BilledCostBlock vaultId={vaultId} />
  </div>;
}

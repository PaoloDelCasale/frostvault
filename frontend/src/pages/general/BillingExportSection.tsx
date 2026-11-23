import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiQueryKeys, fetchBillingExport, type BillingExportStatus } from "@/api";
import { BilledCategoryList, BilledDataDate, BilledStale, BillingExportState, BillingSetupSteps, billedMoney } from "@/components/BilledCost";
import { Panel } from "@/components/Panel";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/lib/errorMessage";
import { formatMonth } from "@/pages/archive/format";
import { vaultSettingsUrl } from "@/router";


type Period = BillingExportStatus["periods"][number];

function PeriodTable({ period, currency }: { period: Period; currency: string | null }) {
  const { t, locale } = useI18n();
  const rows = [
    ...period.vaults.map((vault) => ({ key: `vault-${vault.vault_id}`, label: <a href={vaultSettingsUrl(vault.vault_id, "usage")} className="font-semibold text-ink underline">{vault.name ?? vault.bucket}</a>, entry: vault })),
    ...period.installation.map((entry) => ({ key: `${entry.role}-${entry.bucket}`, label: <span>{t(`billed_cost.role_${entry.role}`, { bucket: entry.bucket })}</span>, entry })),
  ];
  return <div className="grid gap-2" data-testid={`billing-period-${period.period}`}>
    <h3 className="flex flex-wrap justify-between gap-2 font-semibold"><span>{formatMonth(period.period, locale)}</span>
      <span className="tabular-nums">{t("billed_cost.period_total", { cost: billedMoney(period.total, currency, locale) })}</span></h3>
    {rows.length === 0 ? <p className="text-sm text-muted">{t("billed_cost.no_charges")}</p>
      : <ul className="grid">{rows.map((row) => <li key={row.key} className="grid gap-1 border-t border-line py-2" data-testid="billing-row">
        <span className="flex flex-wrap justify-between gap-2 text-sm"><span className="min-w-0 break-words">{row.label}</span>
          <span className="font-semibold tabular-nums">{billedMoney(row.entry.total, currency, locale)}</span></span>
        <details className="text-sm"><summary className="w-fit cursor-pointer text-muted">{t("billed_cost.categories_show")}</summary>
          <BilledCategoryList categories={row.entry.categories} currency={currency} /></details>
      </li>)}</ul>}
  </div>;
}

/**
 * The billing export on the System page (#738): its state, every bucket's
 * Billed Cost once read (Vault Buckets by Vault, the System Bucket and a
 * shared bucket as installation rows) and the one-time setup steps.
 */
export function BillingExportSection() {
  const { t } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const query = useQuery({ queryKey: apiQueryKeys.billingExport, queryFn: fetchBillingExport, retry: false });
  const data = query.data;
  // A Vault page link opens on #billing-export: scroll there once it renders.
  useEffect(() => {
    if (data && window.location.hash === "#billing-export") document.getElementById("billing-export")?.scrollIntoView?.({ block: "start" });
  }, [data]);
  return <section id="billing-export" aria-labelledby="system-billing-heading" className="grid scroll-mt-4 gap-4">
    <div>
      <h2 id="system-billing-heading" className="text-xl font-bold">{t("billed_cost.system_title")}</h2>
      <p className="mt-1 text-sm text-muted">{t("billed_cost.system_summary")}</p>
    </div>
    <Panel className="grid gap-4 p-5"><div className="grid gap-4" data-testid="billing-export">
      {query.error ? <p role="alert" className="text-sm text-destructive">{errorMessage(query.error, translateRef.current)}</p>
        : !data ? <p role="status" className="text-sm text-muted">{t("ui.loading")}</p>
          : <>
            <BillingExportState status={data} />
            {data.state === "ok" && <>
              <BilledStale status={data} />
              {data.periods.map((period) => <PeriodTable key={period.period} period={period} currency={data.currency} />)}
              <BilledDataDate status={data} />
              <p className="text-xs text-muted">{t("billed_cost.not_estimate")}</p>
            </>}
            <details className="rounded-card border border-line" open={data.state !== "ok" && data.state !== "unavailable"}>
              <summary className="min-h-11 cursor-pointer px-3 py-3 font-semibold">{t("billed_cost.setup_title")}</summary>
              <div className="border-t border-line p-3"><BillingSetupSteps bucket={data.bucket} prefix={data.prefix} /></div>
            </details>
          </>}
    </div></Panel>
  </section>;
}

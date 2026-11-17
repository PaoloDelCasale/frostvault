import { useEffect, useRef, useState } from "react";
import { fetchVaultBilledCost, type VaultBilledCost } from "@/api";
import { BilledCategoryList, BilledDataDate, BillingExportState, billedMoney } from "@/components/BilledCost";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/lib/errorMessage";
import { formatApproxMoney, formatMonth } from "@/pages/archive/format";
import { systemUrl } from "@/router";

/**
 * The Vault's Billed Cost from the AWS billing export (#738), by month and
 * category with the date of the data, next to and apart from its Cost
 * Estimate. Without a readable export only the estimate shows, with a link to
 * the setup steps on the System page.
 */
export function BilledCostPanel({ vaultId }: { vaultId: number }) {
  const { t, locale } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const [data, setData] = useState<VaultBilledCost | null>(null);
  const [loadError, setLoadError] = useState("");
  useEffect(() => {
    let live = true;
    setData(null); setLoadError("");
    void fetchVaultBilledCost(vaultId).then((value) => { if (live) setData(value); })
      .catch((error: unknown) => { if (live) setLoadError(errorMessage(error, translateRef.current)); });
    return () => { live = false; };
  }, [vaultId]);

  if (loadError) return <p role="alert" className="text-sm text-destructive">{loadError}</p>;
  if (!data) return <p role="status" className="text-sm text-muted">{t("ui.loading")}</p>;
  const estimate = data.estimate;
  return <div className="grid gap-4 text-sm" data-testid="billed-cost">
    <div className="grid gap-1 rounded-card border border-line p-3" data-testid="billed-cost-billed">
      <h3 className="font-semibold">{t("billed_cost.billed_title")}</h3>
      {data.state === "ok" ? <>
        {data.shared && <p className="text-muted" data-testid="billed-cost-shared">{t("billed_cost.shared_bucket", { bucket: data.vault_bucket })}</p>}
        {data.periods.length === 0 ? <p className="text-muted">{t("billed_cost.no_charges")}</p>
          : data.periods.map((period) => <div key={period.period} className="grid gap-1 border-t border-line pt-2 first-of-type:border-t-0 first-of-type:pt-0" data-testid={`billed-cost-${period.period}`}>
            <p className="flex flex-wrap justify-between gap-2"><span className="font-semibold">{formatMonth(period.period, locale)}</span>
              <span className="font-semibold tabular-nums" data-testid="billed-cost-total">{billedMoney(period.total, data.currency, locale)}</span></p>
            <BilledCategoryList categories={period.categories} currency={data.currency} />
          </div>)}
        <BilledDataDate status={data} />
      </> : <>
        <BillingExportState status={data} />
        <a className="w-fit font-semibold text-ink underline" href={systemUrl("billing-export")} data-testid="billed-cost-setup-link">
          {t(data.state === "error" ? "billed_cost.status_link" : "billed_cost.setup_link")}</a>
      </>}
    </div>
    {estimate && <div className="grid gap-1 rounded-card border border-dashed border-line p-3" data-testid="billed-cost-estimate">
      <h3 className="font-semibold">{t("billed_cost.estimate_title")}</h3>
      <p className="tabular-nums">{t("billed_cost.estimate_month", { cost: formatApproxMoney(estimate.monthly, { currency: estimate.currency, locale }) })}</p>
      <p className="text-xs text-muted">{t("billed_cost.estimate_caveat")}</p>
    </div>}
  </div>;
}

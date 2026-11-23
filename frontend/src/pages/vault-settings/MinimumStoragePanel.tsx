import { useEffect, useRef, useState } from "react";
import { vaultApi, type MinimumStorageSummary } from "@/api";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/lib/errorMessage";
import { formatApproxMoney, formatBytes, formatDate, formatGroupedCount, formatMonth } from "@/pages/archive/format";

/**
 * Minimum Storage Duration of the Vault's versions (#731): per storage class,
 * the versions within and beyond it, what the minimum still bills and, month
 * by month, when the versions within it are released. List-price estimates.
 * Mounted only once its section is opened: the summary reads every version.
 */
export function MinimumStoragePanel({ vaultId }: { vaultId: number }) {
  const { t, locale } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const [summary, setSummary] = useState<MinimumStorageSummary | null>(null);
  const [loadError, setLoadError] = useState("");
  useEffect(() => {
    let live = true;
    setSummary(null); setLoadError("");
    void vaultApi(vaultId).minimumStorage().then((data) => { if (live) setSummary(data); })
      .catch((error: unknown) => { if (live) setLoadError(errorMessage(error, translateRef.current)); });
    return () => { live = false; };
  }, [vaultId]);

  if (loadError) return <p role="alert" className="text-sm text-destructive">{loadError}</p>;
  if (!summary) return <p role="status" className="text-sm text-muted">{t("ui.loading")}</p>;
  const classes = (summary.by_storage_class ?? []).filter((entry) => entry.minimum_days > 0);
  const amount = (count: number, bytes: number) => t("minimum_storage.amount", { count, count_shown: formatGroupedCount(count, locale), bytes: formatBytes(bytes, locale) });
  const money = (value: number) => formatApproxMoney(value, { currency: summary.currency, locale });
  return <div className="grid gap-3 text-sm" data-testid="minimum-storage">
    {classes.length === 0 ? <p data-testid="minimum-storage-none">{t("minimum_storage.none")}</p> : <>
      <div className="overflow-x-auto"><table className="w-full">
        <thead className="text-left text-xs text-muted"><tr>
          <th scope="col" className="py-1 pr-2">{t("minimum_storage.col_class")}</th>
          <th scope="col" className="py-1 pr-2 text-right">{t("minimum_storage.col_minimum")}</th>
          <th scope="col" className="py-1 pr-2 text-right">{t("minimum_storage.col_within")}</th>
          <th scope="col" className="py-1 pr-2 text-right">{t("minimum_storage.col_beyond")}</th>
          <th scope="col" className="py-1 text-right">{t("minimum_storage.col_cost")}</th>
        </tr></thead>
        <tbody>{classes.map((entry) => <tr key={entry.storage_class} className="border-t border-line" data-testid={`minimum-storage-${entry.storage_class}`}>
          <td className="py-1 pr-2">{t(`storage.${entry.storage_class}`)}</td>
          <td className="py-1 pr-2 text-right tabular-nums">{t("minimum_storage.days", { count: entry.minimum_days })}</td>
          <td className="py-1 pr-2 text-right tabular-nums">{amount(entry.within.count, entry.within.bytes)}</td>
          <td className="py-1 pr-2 text-right tabular-nums">{amount(entry.beyond.count, entry.beyond.bytes)}</td>
          <td className="py-1 text-right tabular-nums">{entry.within.count > 0 ? money(entry.remaining_cost) : "—"}</td>
        </tr>)}</tbody>
      </table></div>
      <p className="font-semibold" data-testid="minimum-storage-cost">{summary.remaining_cost > 0
        ? t("minimum_storage.remaining_cost", { cost: money(summary.remaining_cost) })
        : t("minimum_storage.no_remaining")}</p>
      {(summary.releases ?? []).length > 0 && <div className="grid gap-1">
        <h3 className="font-semibold">{t("minimum_storage.releases_title")}</h3>
        <ul className="grid gap-0.5" data-testid="minimum-storage-releases">
          {(summary.releases ?? []).map((release) => <li key={release.month} className="tabular-nums">
            {t("minimum_storage.release", { month: formatMonth(release.month, locale), amount: amount(release.count, release.bytes) })}
          </li>)}
        </ul>
      </div>}
      {summary.estimated_count > 0 && <p className="text-muted" data-testid="minimum-storage-estimated">
        {t("minimum_storage.estimated_versions", { count: summary.estimated_count, count_shown: formatGroupedCount(summary.estimated_count, locale) })}
      </p>}
    </>}
    <p className="text-xs text-muted" data-testid="minimum-storage-disclaimer">{t("minimum_storage.disclaimer", { region: summary.region, date: formatDate(summary.pricing_effective_at, locale) })}</p>
  </div>;
}

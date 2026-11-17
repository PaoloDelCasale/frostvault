import { formatBytes, formatDate, formatGroupedCount, formatMoney } from "@/pages/archive/format";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type MinimumStorageAmount = {
  count: number;
  bytes: number;
  remaining_cost: number;
  savings_from?: string | null;
  estimated_count?: number;
  currency?: string;
  by_storage_class: { storage_class: string; minimum_days: number }[];
};

/**
 * One line where a deletion is decided (#731): how many files are still
 * within their Minimum Storage Duration, that deleting them saves nothing
 * because AWS bills the remaining days anyway, and from when it would save.
 * Always says it is a list-price estimate. Nothing when none is within it.
 */
export function MinimumStorageNote({ amount, t, locale, kind = "files", testId = "minimum-storage-note" }: {
  amount: MinimumStorageAmount | null | undefined;
  t: Translate;
  locale?: string;
  /** «files» for a selection, «versions» for a whole prefix. */
  kind?: "files" | "versions";
  testId?: string;
}) {
  if (!amount || !(amount.count > 0)) return null;
  const classes = amount.by_storage_class.filter((entry) => entry.minimum_days > 0);
  const params = {
    count: amount.count,
    count_shown: formatGroupedCount(amount.count, locale),
    bytes: formatBytes(amount.bytes, locale),
    cost: formatMoney(amount.remaining_cost, { currency: amount.currency, locale }),
    date: formatDate(amount.savings_from, locale),
  };
  const lead = classes.length === 1
    ? t(`minimum_storage.${kind}_in_class`, { ...params, class: t(`storage.${classes[0].storage_class}`), days: classes[0].minimum_days })
    : t(`minimum_storage.${kind}_mixed`, params);
  const estimated = amount.estimated_count ?? 0;
  return <p className="text-sm" role="note" data-testid={testId}>
    {lead} {t(amount.savings_from ? "minimum_storage.no_saving" : "minimum_storage.no_saving_undated", params)}{" "}
    {estimated > 0 ? `${t("minimum_storage.estimated_entry", { count: estimated, count_shown: formatGroupedCount(estimated, locale) })} ` : ""}
    <span className="text-muted">{t("minimum_storage.list_price")}</span>
  </p>;
}

import { formatBytes, formatGroupedCount, formatUtcDate } from "@/pages/archive/format";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type RetentionLockAmount = {
  days: number;
  locked_count: number;
  locked_bytes: number;
  locked_from?: string | null;
  locked_until?: string | null;
};

/**
 * One line where a deletion is decided (#736, ADR-0050): how many files (or
 * versions) a Governance Retention Lock still holds when the deletion runs,
 * and from when they can be deleted. They are deleted from that date, never
 * earlier, and nothing fails meanwhile. Nothing when none is locked.
 */
export function RetentionLockNote({ amount, t, locale, kind = "files", testId = "retention-lock-note" }: {
  amount: RetentionLockAmount | null | undefined;
  t: Translate;
  locale?: string;
  /** «files» for a selection, «versions» for a whole bucket. */
  kind?: "files" | "versions";
  testId?: string;
}) {
  if (!amount || !(amount.locked_count > 0)) return null;
  const from = formatUtcDate(amount.locked_from, locale);
  const until = formatUtcDate(amount.locked_until, locale);
  const params = {
    count: amount.locked_count,
    count_shown: formatGroupedCount(amount.locked_count, locale),
    bytes: formatBytes(amount.locked_bytes, locale),
    from,
    until,
    days: amount.days,
  };
  return <p className="text-sm" role="note" data-testid={testId}>
    {t(`retention_lock.locked_${kind}`, params)}{" "}
    {t(from === until ? "retention_lock.deletable_from" : "retention_lock.deletable_between", params)}{" "}
    <span className="text-muted">{t("retention_lock.estimate")}</span>
  </p>;
}

import { useQuery } from "@tanstack/react-query";
import { apiQueryKeys, readOnMount, vaultApi } from "@/api";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/lib/errorMessage";
import { formatBytes, formatDateTime, formatGroupedCount, formatUtcDate } from "@/pages/archive/format";

/**
 * The Vault's Governance Retention Lock (#736, ADR-0050): read-only, fixed
 * at creation, and the superseded versions it still holds back, deleted by
 * FrostVault from their RetainUntilDate. Versions whose deletion failed say
 * so, with the last error and the next attempt (#749).
 */
export function RetentionLockPanel({ vaultId }: { vaultId: number }) {
  const { t, locale } = useI18n();
  const loaded = useQuery({ queryKey: apiQueryKeys.retentionLock(vaultId), queryFn: () => vaultApi(vaultId).retentionLock(), ...readOnMount });
  const state = loaded.data;

  if (loaded.error) return <p role="alert" className="text-sm text-destructive">{errorMessage(loaded.error, t)}</p>;
  if (!state) return <p role="status" className="text-sm text-muted">{t("ui.loading")}</p>;
  if (!state.days) return <p className="text-sm" data-testid="retention-lock-none">{t("retention_lock.none")}</p>;
  const retained = state.retained;
  return <div className="grid gap-2 text-sm" data-testid="retention-lock">
    <p className="font-semibold" data-testid="retention-lock-mode">{t("retention_lock.active", { count: state.days })}</p>
    <p className="text-muted">{t("retention_lock.fixed")}</p>
    {retained && retained.count > 0
      ? <p data-testid="retention-lock-retained">{t("retention_lock.retained", {
        count: retained.count, count_shown: formatGroupedCount(retained.count, locale),
        bytes: formatBytes(retained.bytes, locale),
        from: formatUtcDate(retained.deletable_from, locale), until: formatUtcDate(retained.deletable_until, locale),
      })}</p>
      : <p data-testid="retention-lock-retained-none">{t("retention_lock.retained_none")}</p>}
    {retained && retained.failed_count > 0
      ? <p role="alert" className="text-destructive" data-testid="retention-lock-failed">{t("retention_lock.failed", {
        count: retained.failed_count, count_shown: formatGroupedCount(retained.failed_count, locale),
        error: retained.last_error ?? "—", attempts: retained.failed_attempts,
        next: formatDateTime(retained.next_attempt_at, locale),
      })}</p>
      : null}
  </div>;
}

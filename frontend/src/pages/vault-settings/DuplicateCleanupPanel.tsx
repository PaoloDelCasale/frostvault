import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiQueryKeys, readOnMount, vaultApi, type DuplicateCleanupStatus } from "@/api";
import { FormField, FormInput } from "@/components/FormField";
import { MinimumStorageNote } from "@/components/MinimumStorageNote";
import { RetentionLockNote } from "@/components/RetentionLockNote";
import { SavedIndicator } from "@/components/SavedIndicator";
import { useSavedFlash } from "@/components/useSavedFlash";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/lib/errorMessage";
import { formatBytes, formatDate, formatDateTime } from "@/lib/format";
import { GIB, numberFormat, parseAmount, whole } from "./cleanupAmounts";

type Draft = { grace: string; files: string; gib: string };
const draftOf = (status: DuplicateCleanupStatus, locale: string): Draft => ({
  grace: String(status.grace_days), files: numberFormat(locale).format(status.cap_files),
  gib: numberFormat(locale, 2).format(Math.round((status.cap_bytes / GIB) * 100) / 100),
});

/**
 * Automatic Duplicate Cleanup of one Vault (#726): off by default. Turning it
 * on, or changing its wait and caps, asks for a fresh sign-in (the API client
 * prompts); turning it off never does.
 */
export function DuplicateCleanupPanel({ vaultId, paused, onNotice }: { vaultId: number; paused?: boolean; onNotice: (message: string, error?: boolean) => void }) {
  const { t, locale } = useI18n();
  const [status, setStatus] = useState<DuplicateCleanupStatus | null>(null);
  const [draft, setDraft] = useState<Draft>({ grace: "", files: "", gib: "" });
  const [busy, setBusy] = useState(false);
  const [enabledSaved, flashEnabledSaved] = useSavedFlash();
  const [minimumSaved, flashMinimumSaved] = useSavedFlash();
  const localeRef = useRef(locale);
  localeRef.current = locale;
  function accept(next: DuplicateCleanupStatus) { setStatus(next); setDraft(draftOf(next, localeRef.current)); }
  const loaded = useQuery({ queryKey: apiQueryKeys.duplicateCleanup(vaultId), queryFn: () => vaultApi(vaultId).duplicateCleanup(), ...readOnMount });
  const loadError = loaded.error ? errorMessage(loaded.error, t) : "";
  useEffect(() => {
    if (loaded.data) accept(loaded.data);
    else setStatus(null);
  }, [loaded.data]);
  // The caps are written in the page language: show them again when it changes.
  const statusRef = useRef(status);
  statusRef.current = status;
  useEffect(() => { if (statusRef.current) setDraft(draftOf(statusRef.current, locale)); }, [locale]);

  const grace = whole(draft.grace), files = parseAmount(draft.files, locale), gib = parseAmount(draft.gib, locale);
  const capBytes = Math.round(gib * GIB);
  const valid = status !== null && grace >= status.grace_days_min && grace <= status.grace_days_max
    && Number.isInteger(files) && files >= 1 && Number.isFinite(gib) && capBytes >= 1;
  // Compared as numbers: «5000» and «5,000» are the same cap.
  const dirty = status !== null && (grace !== status.grace_days || files !== status.cap_files
    || capBytes !== Math.round(parseAmount(draftOf(status, locale).gib, locale) * GIB));

  async function run(action: () => Promise<DuplicateCleanupStatus | void>, done?: () => void) {
    setBusy(true);
    try {
      const next = await action();
      if (next) accept(next);
      done?.();
    } catch (error) { onNotice(errorMessage(error, t), true); }
    finally { setBusy(false); }
  }
  const toggle = () => status && void run(() => vaultApi(vaultId).updateDuplicateCleanup({ enabled: !status.enabled }), flashEnabledSaved);
  // Waiting for the Minimum Storage Duration only deletes less: turning it on
  // asks for no fresh sign-in, turning it off does (#731).
  const toggleMinimum = () => status && void run(() => vaultApi(vaultId).updateDuplicateCleanup({ beyond_minimum: !status.beyond_minimum }), flashMinimumSaved);
  const save = () => valid && dirty && void run(
    () => vaultApi(vaultId).updateDuplicateCleanup({ grace_days: grace, cap_files: files, cap_bytes: capBytes }),
    () => onNotice(t("duplicate_cleanup.saved")));
  const cancel = (groupId: string) => void run(async () => {
    await vaultApi(vaultId).cancelJobGroup(groupId, "cloud-purge");
    onNotice(t("duplicate_cleanup.cancelled"));
    return vaultApi(vaultId).duplicateCleanup();
  });

  const outcome = status?.last_outcome;
  // «No duplicates to delete» only when there were none at all (#729): not
  // while some wait, nor when some were never seen on the server. An older
  // outcome without these counts reads as before.
  const counted = (count: number, bytes?: number) => ({ count, count_shown: numberFormat(locale).format(count), bytes: formatBytes(bytes ?? 0, locale) });
  const outcomeText = !outcome ? "" : outcome.outcome === "none" && (outcome.held_count ?? 0) > 0
    ? t("duplicate_cleanup.outcome.none_held", counted(outcome.held_count ?? 0))
    : outcome.outcome === "none" && (outcome.waiting_count ?? 0) > 0
    ? t("duplicate_cleanup.outcome.none_waiting", counted(outcome.waiting_count ?? 0))
    : outcome.outcome === "none" && (outcome.unseen_count ?? 0) > 0
      ? t("duplicate_cleanup.outcome.none_unseen", counted(outcome.unseen_count ?? 0))
      : t(`duplicate_cleanup.outcome.${outcome.outcome}`, outcome.outcome === "scheduled" || outcome.outcome === "capped"
        ? counted(outcome.file_count ?? 0, outcome.byte_count) : {});
  const current = status?.current, waiting = status?.waiting, ready = status?.ready, unseen = status?.unseen, totals = status?.totals, held = status?.held;
  return <section className="grid gap-3" data-panel="duplicate-cleanup">
    {loadError && <p role="alert" className="text-sm text-destructive">{loadError}</p>}
    <div className="grid gap-1">
      <div className="flex flex-wrap items-center gap-x-3">
        <label className="flex min-h-11 items-center gap-3 font-semibold">
          <input type="checkbox" className="size-5 accent-[var(--green)]" data-testid="duplicate-cleanup-enabled"
            checked={status?.enabled ?? false} disabled={!status || busy} onChange={toggle} />
          {t("duplicate_cleanup.enable")}
        </label>
        <SavedIndicator visible={enabledSaved} />
      </div>
      <p className="text-sm text-muted">{status?.enabled === false ? t("duplicate_cleanup.off_hint") : ""} {t("duplicate_cleanup.reauth_hint")}</p>
      {status?.enabled && paused && <p className="text-sm text-muted">{t("duplicate_cleanup.paused_hint")}</p>}
    </div>
    {status && <div className="grid gap-1 text-sm" data-testid="duplicate-cleanup-counts">
      <p data-testid="duplicate-cleanup-current">{!current ? t("duplicate_cleanup.current_unknown") : current.count === 0
        ? t("duplicate_cleanup.current_none") : t("duplicate_cleanup.current", counted(current.count, current.bytes))}</p>
      {status.enabled && waiting && waiting.count > 0 && <p data-testid="duplicate-cleanup-waiting">{t("duplicate_cleanup.waiting", {
        ...counted(waiting.count, waiting.bytes), eligible: formatDate(waiting.first_eligible_at), earliest: formatDate(waiting.earliest_deletion_at),
      })}</p>}
      {status.enabled && ready && ready.count > 0 && <p data-testid="duplicate-cleanup-ready">{t("duplicate_cleanup.ready", counted(ready.count, ready.bytes))}</p>}
      {status.enabled && status.beyond_minimum && held && held.count > 0 && <p data-testid="duplicate-cleanup-held">{t("duplicate_cleanup.held", counted(held.count, held.bytes))}</p>}
      <MinimumStorageNote amount={status.minimum_storage} t={t} locale={locale} testId="duplicate-cleanup-minimum" />
      <RetentionLockNote amount={status.retention_lock} t={t} locale={locale} testId="duplicate-cleanup-retention-lock" />
      {unseen && unseen.count > 0 && <p data-testid="duplicate-cleanup-unseen">{t("duplicate_cleanup.unseen", counted(unseen.count, unseen.bytes))}</p>}
      {totals && totals.deleted_count > 0 && <p data-testid="duplicate-cleanup-totals">{t("duplicate_cleanup.totals", {
        ...counted(totals.deleted_count, totals.deleted_bytes), when: formatDate(totals.last_deleted_at),
      })}</p>}
    </div>}
    {status?.enabled && <>
      <div className="grid gap-1">
        <div className="flex flex-wrap items-center gap-x-3">
          <label className="flex min-h-11 items-center gap-3 font-semibold">
            <input type="checkbox" className="size-5 accent-[var(--green)]" data-testid="duplicate-cleanup-beyond-minimum"
              checked={status.beyond_minimum ?? false} disabled={busy} onChange={toggleMinimum} />
            {t("duplicate_cleanup.beyond_minimum")}
          </label>
          <SavedIndicator visible={minimumSaved} />
        </div>
        <p className="text-sm text-muted">{t("duplicate_cleanup.beyond_minimum_help")}</p>
      </div>
      <form className="grid gap-3 sm:grid-cols-3" onSubmit={(event) => { event.preventDefault(); save(); }}>
        <FormField label={t("duplicate_cleanup.grace_days")} htmlFor="duplicate-grace">
          <FormInput id="duplicate-grace" inputMode="numeric" value={draft.grace} disabled={busy}
            onChange={(event) => setDraft({ ...draft, grace: event.target.value })} />
        </FormField>
        <FormField label={t("duplicate_cleanup.cap_files")} htmlFor="duplicate-cap-files">
          <FormInput id="duplicate-cap-files" inputMode="numeric" value={draft.files} disabled={busy}
            onChange={(event) => setDraft({ ...draft, files: event.target.value })} />
        </FormField>
        <FormField label={t("duplicate_cleanup.cap_gib")} htmlFor="duplicate-cap-gib">
          <FormInput id="duplicate-cap-gib" inputMode="decimal" value={draft.gib} disabled={busy}
            onChange={(event) => setDraft({ ...draft, gib: event.target.value })} />
        </FormField>
        <p className="text-sm text-muted sm:col-span-3">{t("duplicate_cleanup.grace_help", { min: status.grace_days_min, max: status.grace_days_max })} {t("duplicate_cleanup.cap_help")}</p>
        <Button type="submit" variant={dirty && valid ? "primary" : "secondary"} className="min-h-11 w-full sm:w-fit" disabled={busy || !dirty || !valid}>
          {t("duplicate_cleanup.save")}
        </Button>
      </form>
      <p className="text-sm" data-testid="duplicate-cleanup-last-run">
        {status.last_run_at ? t("duplicate_cleanup.last_run", { when: formatDateTime(status.last_run_at), outcome: outcomeText }) : t("duplicate_cleanup.never_run")}
        {status.next_run_at ? ` ${t("duplicate_cleanup.next_run", { when: formatDateTime(status.next_run_at) })}` : ""}
      </p>
    </>}
    {status?.pending.map((group) => <div key={group.group_id} className="grid gap-2 rounded-card border border-line p-3 text-sm" data-testid="duplicate-cleanup-pending">
      <p>{t("duplicate_cleanup.pending", { ...counted(group.file_count, group.byte_count), when: formatDateTime(group.run_after) })}</p>
      {group.cancellable && <Button type="button" variant="secondary" className="min-h-11 w-fit" disabled={busy} onClick={() => cancel(group.group_id)}>{t("duplicate_cleanup.cancel")}</Button>}
    </div>)}
  </section>;
}

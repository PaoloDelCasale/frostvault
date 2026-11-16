import { useEffect, useRef, useState } from "react";
import { cancelCloudDeletionGroup, fetchDuplicateCleanup, updateDuplicateCleanup, type DuplicateCleanupStatus } from "@/api";
import { FormField, FormInput } from "@/components/FormField";
import { SavedIndicator } from "@/components/SavedIndicator";
import { useSavedFlash } from "@/components/useSavedFlash";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/lib/errorMessage";
import { formatBytes, formatDateTime } from "@/pages/archive/format";

const GIB = 1024 ** 3;
type Draft = { grace: string; files: string; gib: string };
const draftOf = (status: DuplicateCleanupStatus): Draft => ({
  grace: String(status.grace_days), files: String(status.cap_files),
  gib: String(Math.round((status.cap_bytes / GIB) * 100) / 100),
});
const whole = (value: string) => (/^\d+$/.test(value.trim()) ? Number(value) : NaN);

/**
 * Automatic Duplicate Cleanup of one Vault (#726): off by default. Turning it
 * on, or changing its wait and caps, asks for a fresh sign-in (the API client
 * prompts); turning it off never does.
 */
export function DuplicateCleanupPanel({ vaultId, paused, onNotice }: { vaultId: number; paused?: boolean; onNotice: (message: string, error?: boolean) => void }) {
  const { t } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const [status, setStatus] = useState<DuplicateCleanupStatus | null>(null);
  const [draft, setDraft] = useState<Draft>({ grace: "", files: "", gib: "" });
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [enabledSaved, flashEnabledSaved] = useSavedFlash();
  function accept(next: DuplicateCleanupStatus) { setStatus(next); setDraft(draftOf(next)); }
  useEffect(() => {
    let live = true;
    setStatus(null); setLoadError("");
    void fetchDuplicateCleanup(vaultId).then((data) => { if (live) accept(data); })
      .catch((error: unknown) => { if (live) setLoadError(errorMessage(error, translateRef.current)); });
    return () => { live = false; };
  }, [vaultId]);

  const grace = whole(draft.grace), files = whole(draft.files), gib = Number(draft.gib.replace(",", "."));
  const capBytes = Math.round(gib * GIB);
  const valid = status !== null && grace >= status.grace_days_min && grace <= status.grace_days_max
    && files >= 1 && Number.isFinite(gib) && capBytes >= 1;
  const dirty = status !== null && (draft.grace !== String(status.grace_days) || draft.files !== String(status.cap_files)
    || draft.gib !== draftOf(status).gib);

  async function run(action: () => Promise<DuplicateCleanupStatus | void>, done?: () => void) {
    setBusy(true);
    try {
      const next = await action();
      if (next) accept(next);
      done?.();
    } catch (error) { onNotice(errorMessage(error, t), true); }
    finally { setBusy(false); }
  }
  const toggle = () => status && void run(() => updateDuplicateCleanup(vaultId, { enabled: !status.enabled }), flashEnabledSaved);
  const save = () => valid && dirty && void run(
    () => updateDuplicateCleanup(vaultId, { grace_days: grace, cap_files: files, cap_bytes: capBytes }),
    () => onNotice(t("duplicate_cleanup.saved")));
  const cancel = (groupId: string) => void run(async () => {
    await cancelCloudDeletionGroup(vaultId, groupId);
    onNotice(t("duplicate_cleanup.cancelled"));
    return fetchDuplicateCleanup(vaultId);
  });

  const outcome = status?.last_outcome;
  const outcomeText = outcome ? t(`duplicate_cleanup.outcome.${outcome.outcome}`, outcome.outcome === "scheduled" || outcome.outcome === "capped"
    ? { count: outcome.file_count ?? 0, bytes: formatBytes(outcome.byte_count ?? 0) } : {}) : "";
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
    {status?.enabled && <>
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
      <p>{t("duplicate_cleanup.pending", { count: group.file_count, bytes: formatBytes(group.byte_count), when: formatDateTime(group.run_after) })}</p>
      {group.cancellable && <Button type="button" variant="secondary" className="min-h-11 w-fit" disabled={busy} onClick={() => cancel(group.group_id)}>{t("duplicate_cleanup.cancel")}</Button>}
    </div>)}
  </section>;
}

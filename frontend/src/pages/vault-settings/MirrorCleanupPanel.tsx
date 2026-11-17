import { useEffect, useRef, useState } from "react";
import { cancelCloudDeletionGroup, fetchMirrorCleanup, updateMirrorCleanup, type MirrorCleanupStatus } from "@/api";
import { FormField, FormInput } from "@/components/FormField";
import { MinimumStorageNote } from "@/components/MinimumStorageNote";
import { RetentionLockNote } from "@/components/RetentionLockNote";
import { SavedIndicator } from "@/components/SavedIndicator";
import { useSavedFlash } from "@/components/useSavedFlash";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/lib/errorMessage";
import { formatBytes, formatDate, formatDateTime } from "@/pages/archive/format";
import { GIB, numberFormat, parseAmount, whole } from "./cleanupAmounts";

type Draft = { grace: string; files: string; gib: string; folders: string };
const draftOf = (status: MirrorCleanupStatus, locale: string): Draft => ({
  grace: String(status.grace_days), files: numberFormat(locale).format(status.cap_files),
  gib: numberFormat(locale, 2).format(Math.round((status.cap_bytes / GIB) * 100) / 100),
  folders: status.folders.join("\n"),
});
/** One folder per line; blank lines dropped. The server validates and normalizes them. */
const foldersOf = (text: string) => text.split("\n").map((line) => line.trim()).filter(Boolean);

/**
 * Mirror Cleanup of one Vault (#771, ADR-0052): off by default. It deletes
 * from the cloud the Cloud-only files gone from the server for N days, which
 * may be the only copy of their content. Turning it on, or making it delete
 * more, asks for a fresh sign-in (the API client prompts); turning it off or
 * narrowing it never does.
 */
export function MirrorCleanupPanel({ vaultId, paused, onNotice }: { vaultId: number; paused?: boolean; onNotice: (message: string, error?: boolean) => void }) {
  const { t, locale } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const [status, setStatus] = useState<MirrorCleanupStatus | null>(null);
  const [draft, setDraft] = useState<Draft>({ grace: "", files: "", gib: "", folders: "" });
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [enabledSaved, flashEnabledSaved] = useSavedFlash();
  const [minimumSaved, flashMinimumSaved] = useSavedFlash();
  const localeRef = useRef(locale);
  localeRef.current = locale;
  function accept(next: MirrorCleanupStatus) { setStatus(next); setDraft(draftOf(next, localeRef.current)); }
  useEffect(() => {
    let live = true;
    setStatus(null); setLoadError("");
    void fetchMirrorCleanup(vaultId).then((data) => { if (live) accept(data); })
      .catch((error: unknown) => { if (live) setLoadError(errorMessage(error, translateRef.current)); });
    return () => { live = false; };
  }, [vaultId]);
  const statusRef = useRef(status);
  statusRef.current = status;
  useEffect(() => { if (statusRef.current) setDraft(draftOf(statusRef.current, locale)); }, [locale]);

  const grace = whole(draft.grace), files = parseAmount(draft.files, locale), gib = parseAmount(draft.gib, locale);
  const capBytes = Math.round(gib * GIB);
  const folders = foldersOf(draft.folders);
  const valid = status !== null && grace >= status.grace_days_min && grace <= status.grace_days_max
    && Number.isInteger(files) && files >= 1 && Number.isFinite(gib) && capBytes >= 1;
  const dirty = status !== null && (grace !== status.grace_days || files !== status.cap_files
    || capBytes !== Math.round(parseAmount(draftOf(status, locale).gib, locale) * GIB)
    || folders.join("\n") !== status.folders.join("\n"));

  async function run(action: () => Promise<MirrorCleanupStatus | void>, done?: () => void) {
    setBusy(true);
    try {
      const next = await action();
      if (next) accept(next);
      done?.();
    } catch (error) { onNotice(errorMessage(error, t), true); }
    finally { setBusy(false); }
  }
  const toggle = () => status && void run(() => updateMirrorCleanup(vaultId, { enabled: !status.enabled }), flashEnabledSaved);
  const toggleMinimum = () => status && void run(() => updateMirrorCleanup(vaultId, { beyond_minimum: !status.beyond_minimum }), flashMinimumSaved);
  const save = () => valid && dirty && void run(
    () => updateMirrorCleanup(vaultId, { grace_days: grace, cap_files: files, cap_bytes: capBytes, folders }),
    () => onNotice(t("mirror_cleanup.saved")));
  const cancel = (groupId: string) => void run(async () => {
    await cancelCloudDeletionGroup(vaultId, groupId);
    onNotice(t("mirror_cleanup.cancelled"));
    return fetchMirrorCleanup(vaultId);
  });

  const outcome = status?.last_outcome;
  const counted = (count: number, bytes?: number) => ({ count, count_shown: numberFormat(locale).format(count), bytes: formatBytes(bytes ?? 0, locale) });
  const outcomeText = !outcome ? "" : outcome.outcome === "none" && (outcome.held_count ?? 0) > 0
    ? t("mirror_cleanup.outcome.none_held", counted(outcome.held_count ?? 0))
    : outcome.outcome === "none" && (outcome.waiting_count ?? 0) > 0
      ? t("mirror_cleanup.outcome.none_waiting", counted(outcome.waiting_count ?? 0))
      : outcome.outcome === "none" && (outcome.unseen_count ?? 0) > 0
        ? t("mirror_cleanup.outcome.none_unseen", counted(outcome.unseen_count ?? 0))
        : t(`mirror_cleanup.outcome.${outcome.outcome}`, outcome.outcome === "scheduled" || outcome.outcome === "capped"
          ? counted(outcome.file_count ?? 0, outcome.byte_count) : {});
  const current = status?.current, waiting = status?.waiting, ready = status?.ready, unseen = status?.unseen, totals = status?.totals, held = status?.held;
  const life = status?.cloud_life;
  return <section className="grid gap-3" data-panel="mirror-cleanup">
    {loadError && <p role="alert" className="text-sm text-destructive">{loadError}</p>}
    <p role="note" className="rounded-card border border-[var(--state-problem-bg)] p-3 text-sm font-semibold" data-testid="mirror-cleanup-warning">{t("mirror_cleanup.warning")}</p>
    <div className="grid gap-1">
      <div className="flex flex-wrap items-center gap-x-3">
        <label className="flex min-h-11 items-center gap-3 font-semibold">
          <input type="checkbox" className="size-5 accent-[var(--green)]" data-testid="mirror-cleanup-enabled"
            checked={status?.enabled ?? false} disabled={!status || busy} onChange={toggle} />
          {t("mirror_cleanup.enable")}
        </label>
        <SavedIndicator visible={enabledSaved} />
      </div>
      <p className="text-sm text-muted">{status?.enabled === false ? t("mirror_cleanup.off_hint") : ""} {t("mirror_cleanup.reauth_hint")}</p>
      {status?.enabled && paused && <p className="text-sm text-muted">{t("mirror_cleanup.paused_hint")}</p>}
    </div>
    {status && <div className="grid gap-1 text-sm" data-testid="mirror-cleanup-counts">
      <p data-testid="mirror-cleanup-scope">{status.folders.length === 0 ? t("mirror_cleanup.folders_whole") : t("mirror_cleanup.folders_some", { folders: status.folders.join(", ") })}</p>
      <p data-testid="mirror-cleanup-current">{!current ? t("mirror_cleanup.current_unknown") : current.count === 0
        ? t("mirror_cleanup.current_none") : t("mirror_cleanup.current", counted(current.count, current.bytes))}</p>
      {status.enabled && waiting && waiting.count > 0 && <p data-testid="mirror-cleanup-waiting">{t("mirror_cleanup.waiting", {
        ...counted(waiting.count, waiting.bytes), eligible: formatDate(waiting.first_eligible_at), earliest: formatDate(waiting.earliest_deletion_at),
      })}</p>}
      {status.enabled && ready && ready.count > 0 && <p data-testid="mirror-cleanup-ready">{t("mirror_cleanup.ready", counted(ready.count, ready.bytes))}</p>}
      {status.enabled && status.beyond_minimum && held && held.count > 0 && <p data-testid="mirror-cleanup-held">{t("mirror_cleanup.held", counted(held.count, held.bytes))}</p>}
      <MinimumStorageNote amount={status.minimum_storage} t={t} locale={locale} testId="mirror-cleanup-minimum" />
      <RetentionLockNote amount={status.retention_lock} t={t} locale={locale} testId="mirror-cleanup-retention-lock" />
      {unseen && unseen.count > 0 && <p data-testid="mirror-cleanup-unseen">{t("mirror_cleanup.unseen", counted(unseen.count, unseen.bytes))}</p>}
      {totals && totals.deleted_count > 0 && <p data-testid="mirror-cleanup-totals">{t("mirror_cleanup.totals", {
        ...counted(totals.deleted_count, totals.deleted_bytes), when: formatDate(totals.last_deleted_at),
      })}</p>}
      {life && <p data-testid="mirror-cleanup-cloud-life">{t("mirror_cleanup.cloud_life", { days: life.days_after_disappearance, grace: status.grace_days })}
        {life.minimum_days > 0 && life.minimum_until_day !== null && ` ${t(life.beyond_minimum ? "mirror_cleanup.cloud_life_beyond" : "mirror_cleanup.cloud_life_within", {
          storage_class: life.storage_class, minimum: life.minimum_days, until: life.minimum_until_day,
        })}`}</p>}
    </div>}
    {status?.enabled && <>
      <div className="grid gap-1">
        <div className="flex flex-wrap items-center gap-x-3">
          <label className="flex min-h-11 items-center gap-3 font-semibold">
            <input type="checkbox" className="size-5 accent-[var(--green)]" data-testid="mirror-cleanup-beyond-minimum"
              checked={status.beyond_minimum ?? false} disabled={busy} onChange={toggleMinimum} />
            {t("mirror_cleanup.beyond_minimum")}
          </label>
          <SavedIndicator visible={minimumSaved} />
        </div>
        <p className="text-sm text-muted">{t("mirror_cleanup.beyond_minimum_help")}</p>
      </div>
      <form className="grid gap-3 sm:grid-cols-3" onSubmit={(event) => { event.preventDefault(); save(); }}>
        <FormField label={t("mirror_cleanup.grace_days")} htmlFor="mirror-grace">
          <FormInput id="mirror-grace" inputMode="numeric" value={draft.grace} disabled={busy}
            onChange={(event) => setDraft({ ...draft, grace: event.target.value })} />
        </FormField>
        <FormField label={t("mirror_cleanup.cap_files")} htmlFor="mirror-cap-files">
          <FormInput id="mirror-cap-files" inputMode="numeric" value={draft.files} disabled={busy}
            onChange={(event) => setDraft({ ...draft, files: event.target.value })} />
        </FormField>
        <FormField label={t("mirror_cleanup.cap_gib")} htmlFor="mirror-cap-gib">
          <FormInput id="mirror-cap-gib" inputMode="decimal" value={draft.gib} disabled={busy}
            onChange={(event) => setDraft({ ...draft, gib: event.target.value })} />
        </FormField>
        <p className="text-sm text-muted sm:col-span-3">{t("mirror_cleanup.grace_help", { min: status.grace_days_min, max: status.grace_days_max })} {t("mirror_cleanup.cap_help")}</p>
        <label className="grid gap-1 text-sm font-semibold sm:col-span-3" htmlFor="mirror-folders">{t("mirror_cleanup.folders")}
          <textarea id="mirror-folders" data-testid="mirror-cleanup-folders" className="min-h-24 w-full min-w-0 rounded-lg border border-input bg-surface p-3 font-mono text-sm font-normal"
            value={draft.folders} disabled={busy} onChange={(event) => setDraft({ ...draft, folders: event.target.value })} />
          <span className="font-normal text-muted">{t("mirror_cleanup.folders_help")}</span>
        </label>
        <Button type="submit" variant={dirty && valid ? "primary" : "secondary"} className="min-h-11 w-full sm:w-fit" disabled={busy || !dirty || !valid}>
          {t("mirror_cleanup.save")}
        </Button>
      </form>
      <p className="text-sm" data-testid="mirror-cleanup-last-run">
        {status.last_run_at ? t("mirror_cleanup.last_run", { when: formatDateTime(status.last_run_at), outcome: outcomeText }) : t("mirror_cleanup.never_run")}
        {status.next_run_at ? ` ${t("mirror_cleanup.next_run", { when: formatDateTime(status.next_run_at) })}` : ""}
      </p>
    </>}
    {status?.pending.map((group) => <div key={group.group_id} className="grid gap-2 rounded-card border border-line p-3 text-sm" data-testid="mirror-cleanup-pending">
      <p>{t("mirror_cleanup.pending", { ...counted(group.file_count, group.byte_count), when: formatDateTime(group.run_after) })}</p>
      {group.cancellable && <Button type="button" variant="secondary" className="min-h-11 w-fit" disabled={busy} onClick={() => cancel(group.group_id)}>{t("mirror_cleanup.cancel")}</Button>}
    </div>)}
  </section>;
}

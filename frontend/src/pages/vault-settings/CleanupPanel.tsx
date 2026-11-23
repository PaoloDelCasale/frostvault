import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiQueryKeys, readOnMount, vaultApi, type DuplicateCleanupStatus, type MirrorCleanupStatus } from "@/api";
import { FormField, FormInput } from "@/components/FormField";
import { MinimumStorageNote } from "@/components/MinimumStorageNote";
import { RetentionLockNote } from "@/components/RetentionLockNote";
import { SavedIndicator } from "@/components/SavedIndicator";
import { useSavedFlash } from "@/components/useSavedFlash";
import { Button } from "@/components/ui/button";
import { useI18n, type MessageParams } from "@/i18n";
import { errorMessage } from "@/lib/errorMessage";
import { formatBytes, formatDate, formatDateTime } from "@/lib/format";
import { GIB, numberFormat, parseAmount, whole } from "./cleanupAmounts";

export type CleanupKind = "duplicate" | "mirror";
type Status = DuplicateCleanupStatus | MirrorCleanupStatus;
type Update = Parameters<ReturnType<typeof vaultApi>["updateMirrorCleanup"]>[0];
/** The requests of each cleanup: its own query key, read and update. */
const API: Record<CleanupKind, {
  key: (vaultId: number) => readonly unknown[];
  read: (vaultId: number) => Promise<Status>;
  update: (vaultId: number, payload: Update) => Promise<Status>;
}> = {
  duplicate: {
    key: apiQueryKeys.duplicateCleanup,
    read: (vaultId) => vaultApi(vaultId).duplicateCleanup(),
    update: (vaultId, payload) => vaultApi(vaultId).updateDuplicateCleanup(payload),
  },
  mirror: {
    key: apiQueryKeys.mirrorCleanup,
    read: (vaultId) => vaultApi(vaultId).mirrorCleanup(),
    update: (vaultId, payload) => vaultApi(vaultId).updateMirrorCleanup(payload),
  },
};
/** Only Mirror Cleanup has a scope of folders and a cloud life (#771). */
const scoped = (status: Status | null): MirrorCleanupStatus | null => (status && "folders" in status ? status : null);

type Draft = { grace: string; files: string; gib: string; folders: string };
const draftOf = (status: Status, locale: string): Draft => ({
  grace: String(status.grace_days), files: numberFormat(locale).format(status.cap_files),
  gib: numberFormat(locale, 2).format(Math.round((status.cap_bytes / GIB) * 100) / 100),
  folders: (scoped(status)?.folders ?? []).join("\n"),
});
/** One folder per line; blank lines dropped. The server validates and normalizes them. */
const foldersOf = (text: string) => text.split("\n").map((line) => line.trim()).filter(Boolean);

/**
 * One automatic cloud cleanup of one Vault, off by default.
 *
 * `duplicate`: Automatic Duplicate Cleanup (#726). Turning it on, or changing
 * its wait and caps, asks for a fresh sign-in (the API client prompts);
 * turning it off never does.
 *
 * `mirror`: Mirror Cleanup (#771, ADR-0052). It deletes from the cloud the
 * Cloud-only files gone from the server for N days, which may be the only
 * copy of their content: it adds a warning, the folders of its scope and the
 * cloud life. Turning it on, or making it delete more, asks for a fresh
 * sign-in; turning it off or narrowing it never does.
 */
export function CleanupPanel({ kind, vaultId, paused, onNotice }: { kind: CleanupKind; vaultId: number; paused?: boolean; onNotice: (message: string, error?: boolean) => void }) {
  const { t, locale } = useI18n();
  const { key, read, update } = API[kind];
  const text = (key: string, params?: MessageParams) => t(`${kind}_cleanup.${key}`, params);
  const testId = (name: string) => `${kind}-cleanup-${name}`;
  const [status, setStatus] = useState<Status | null>(null);
  const [draft, setDraft] = useState<Draft>({ grace: "", files: "", gib: "", folders: "" });
  const [busy, setBusy] = useState(false);
  const [enabledSaved, flashEnabledSaved] = useSavedFlash();
  const [minimumSaved, flashMinimumSaved] = useSavedFlash();
  const localeRef = useRef(locale);
  localeRef.current = locale;
  function accept(next: Status) { setStatus(next); setDraft(draftOf(next, localeRef.current)); }
  const loaded = useQuery({ queryKey: key(vaultId), queryFn: () => read(vaultId), ...readOnMount });
  const loadError = loaded.error ? errorMessage(loaded.error, t) : "";
  useEffect(() => {
    if (loaded.data) accept(loaded.data);
    else setStatus(null);
  }, [loaded.data]);
  // The caps are written in the page language: show them again when it changes.
  const statusRef = useRef(status);
  statusRef.current = status;
  useEffect(() => { if (statusRef.current) setDraft(draftOf(statusRef.current, locale)); }, [locale]);

  const scope = scoped(status);
  const grace = whole(draft.grace), files = parseAmount(draft.files, locale), gib = parseAmount(draft.gib, locale);
  const capBytes = Math.round(gib * GIB);
  const folders = foldersOf(draft.folders);
  const valid = status !== null && grace >= status.grace_days_min && grace <= status.grace_days_max
    && Number.isInteger(files) && files >= 1 && Number.isFinite(gib) && capBytes >= 1;
  // Compared as numbers: «5000» and «5,000» are the same cap.
  const dirty = status !== null && (grace !== status.grace_days || files !== status.cap_files
    || capBytes !== Math.round(parseAmount(draftOf(status, locale).gib, locale) * GIB)
    || (scope !== null && folders.join("\n") !== scope.folders.join("\n")));

  async function run(action: () => Promise<Status | void>, done?: () => void) {
    setBusy(true);
    try {
      const next = await action();
      if (next) accept(next);
      done?.();
    } catch (error) { onNotice(errorMessage(error, t), true); }
    finally { setBusy(false); }
  }
  const toggle = () => status && void run(() => update(vaultId, { enabled: !status.enabled }), flashEnabledSaved);
  // Waiting for the Minimum Storage Duration only deletes less: turning it on
  // asks for no fresh sign-in, turning it off does (#731).
  const toggleMinimum = () => status && void run(() => update(vaultId, { beyond_minimum: !status.beyond_minimum }), flashMinimumSaved);
  const save = () => valid && dirty && void run(
    () => update(vaultId, { grace_days: grace, cap_files: files, cap_bytes: capBytes, ...(scope ? { folders } : {}) }),
    () => onNotice(text("saved")));
  const cancel = (groupId: string) => void run(async () => {
    await vaultApi(vaultId).cancelJobGroup(groupId, "cloud-purge");
    onNotice(text("cancelled"));
    return read(vaultId);
  });

  const outcome = status?.last_outcome;
  // «Nothing to delete» only when there was nothing at all (#729): not while
  // some wait, nor when some were never seen on the server. An older outcome
  // without these counts reads as before.
  const counted = (count: number, bytes?: number) => ({ count, count_shown: numberFormat(locale).format(count), bytes: formatBytes(bytes ?? 0, locale) });
  const outcomeText = !outcome ? "" : outcome.outcome === "none" && (outcome.held_count ?? 0) > 0
    ? text("outcome.none_held", counted(outcome.held_count ?? 0))
    : outcome.outcome === "none" && (outcome.waiting_count ?? 0) > 0
      ? text("outcome.none_waiting", counted(outcome.waiting_count ?? 0))
      : outcome.outcome === "none" && (outcome.unseen_count ?? 0) > 0
        ? text("outcome.none_unseen", counted(outcome.unseen_count ?? 0))
        : text(`outcome.${outcome.outcome}`, outcome.outcome === "scheduled" || outcome.outcome === "capped"
          ? counted(outcome.file_count ?? 0, outcome.byte_count) : {});
  const current = status?.current, waiting = status?.waiting, ready = status?.ready, unseen = status?.unseen, totals = status?.totals, held = status?.held;
  const life = scope?.cloud_life;
  return <section className="grid gap-3" data-panel={`${kind}-cleanup`}>
    {loadError && <p role="alert" className="text-sm text-destructive">{loadError}</p>}
    {kind === "mirror" && <p role="note" className="rounded-card border border-[var(--state-problem-bg)] p-3 text-sm font-semibold" data-testid={testId("warning")}>{text("warning")}</p>}
    <div className="grid gap-1">
      <div className="flex flex-wrap items-center gap-x-3">
        <label className="flex min-h-11 items-center gap-3 font-semibold">
          <input type="checkbox" className="size-5 accent-[var(--green)]" data-testid={testId("enabled")}
            checked={status?.enabled ?? false} disabled={!status || busy} onChange={toggle} />
          {text("enable")}
        </label>
        <SavedIndicator visible={enabledSaved} />
      </div>
      <p className="text-sm text-muted">{status?.enabled === false ? text("off_hint") : ""} {text("reauth_hint")}</p>
      {status?.enabled && paused && <p className="text-sm text-muted">{text("paused_hint")}</p>}
    </div>
    {status && <div className="grid gap-1 text-sm" data-testid={testId("counts")}>
      {scope && <p data-testid={testId("scope")}>{scope.folders.length === 0 ? text("folders_whole") : text("folders_some", { folders: scope.folders.join(", ") })}</p>}
      <p data-testid={testId("current")}>{!current ? text("current_unknown") : current.count === 0
        ? text("current_none") : text("current", counted(current.count, current.bytes))}</p>
      {status.enabled && waiting && waiting.count > 0 && <p data-testid={testId("waiting")}>{text("waiting", {
        ...counted(waiting.count, waiting.bytes), eligible: formatDate(waiting.first_eligible_at), earliest: formatDate(waiting.earliest_deletion_at),
      })}</p>}
      {status.enabled && ready && ready.count > 0 && <p data-testid={testId("ready")}>{text("ready", counted(ready.count, ready.bytes))}</p>}
      {status.enabled && status.beyond_minimum && held && held.count > 0 && <p data-testid={testId("held")}>{text("held", counted(held.count, held.bytes))}</p>}
      <MinimumStorageNote amount={status.minimum_storage} t={t} locale={locale} testId={testId("minimum")} />
      <RetentionLockNote amount={status.retention_lock} t={t} locale={locale} testId={testId("retention-lock")} />
      {unseen && unseen.count > 0 && <p data-testid={testId("unseen")}>{text("unseen", counted(unseen.count, unseen.bytes))}</p>}
      {totals && totals.deleted_count > 0 && <p data-testid={testId("totals")}>{text("totals", {
        ...counted(totals.deleted_count, totals.deleted_bytes), when: formatDate(totals.last_deleted_at),
      })}</p>}
      {life && <p data-testid={testId("cloud-life")}>{text("cloud_life", { days: life.days_after_disappearance, grace: status.grace_days })}
        {life.minimum_days > 0 && life.minimum_until_day !== null && ` ${text(life.beyond_minimum ? "cloud_life_beyond" : "cloud_life_within", {
          storage_class: life.storage_class, minimum: life.minimum_days, until: life.minimum_until_day,
        })}`}</p>}
    </div>}
    {status?.enabled && <>
      <div className="grid gap-1">
        <div className="flex flex-wrap items-center gap-x-3">
          <label className="flex min-h-11 items-center gap-3 font-semibold">
            <input type="checkbox" className="size-5 accent-[var(--green)]" data-testid={testId("beyond-minimum")}
              checked={status.beyond_minimum ?? false} disabled={busy} onChange={toggleMinimum} />
            {text("beyond_minimum")}
          </label>
          <SavedIndicator visible={minimumSaved} />
        </div>
        <p className="text-sm text-muted">{text("beyond_minimum_help")}</p>
      </div>
      <form className="grid gap-3 sm:grid-cols-3" onSubmit={(event) => { event.preventDefault(); save(); }}>
        <FormField label={text("grace_days")} htmlFor={`${kind}-grace`}>
          <FormInput id={`${kind}-grace`} inputMode="numeric" value={draft.grace} disabled={busy}
            onChange={(event) => setDraft({ ...draft, grace: event.target.value })} />
        </FormField>
        <FormField label={text("cap_files")} htmlFor={`${kind}-cap-files`}>
          <FormInput id={`${kind}-cap-files`} inputMode="numeric" value={draft.files} disabled={busy}
            onChange={(event) => setDraft({ ...draft, files: event.target.value })} />
        </FormField>
        <FormField label={text("cap_gib")} htmlFor={`${kind}-cap-gib`}>
          <FormInput id={`${kind}-cap-gib`} inputMode="decimal" value={draft.gib} disabled={busy}
            onChange={(event) => setDraft({ ...draft, gib: event.target.value })} />
        </FormField>
        <p className="text-sm text-muted sm:col-span-3">{text("grace_help", { min: status.grace_days_min, max: status.grace_days_max })} {text("cap_help")}</p>
        {scope && <label className="grid gap-1 text-sm font-semibold sm:col-span-3" htmlFor={`${kind}-folders`}>{text("folders")}
          <textarea id={`${kind}-folders`} data-testid={testId("folders")} className="min-h-24 w-full min-w-0 rounded-lg border border-input bg-surface p-3 font-mono text-sm font-normal"
            value={draft.folders} disabled={busy} onChange={(event) => setDraft({ ...draft, folders: event.target.value })} />
          <span className="font-normal text-muted">{text("folders_help")}</span>
        </label>}
        <Button type="submit" variant={dirty && valid ? "primary" : "secondary"} className="min-h-11 w-full sm:w-fit" disabled={busy || !dirty || !valid}>
          {text("save")}
        </Button>
      </form>
      <p className="text-sm" data-testid={testId("last-run")}>
        {status.last_run_at ? text("last_run", { when: formatDateTime(status.last_run_at), outcome: outcomeText }) : text("never_run")}
        {status.next_run_at ? ` ${text("next_run", { when: formatDateTime(status.next_run_at) })}` : ""}
      </p>
    </>}
    {status?.pending.map((group) => <div key={group.group_id} className="grid gap-2 rounded-card border border-line p-3 text-sm" data-testid={testId("pending")}>
      <p>{text("pending", { ...counted(group.file_count, group.byte_count), when: formatDateTime(group.run_after) })}</p>
      {group.cancellable && <Button type="button" variant="secondary" className="min-h-11 w-fit" disabled={busy} onClick={() => cancel(group.group_id)}>{text("cancel")}</Button>}
    </div>)}
  </section>;
}

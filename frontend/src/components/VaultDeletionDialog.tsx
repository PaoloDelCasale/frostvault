import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError, vaultApi, type VaultDeletionPreview, type VaultDeletionStatus } from "@/api";
import { Dialog } from "@/components/Dialog";
import { FormField, FormInput } from "@/components/FormField";
import { MinimumStorageNote } from "@/components/MinimumStorageNote";
import { RetentionLockNote } from "@/components/RetentionLockNote";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/lib/errorMessage";
import { formatBytes, formatDateTime, formatGroupedCount, formatMoney, formatUtcDate } from "@/lib/format";

const HOUR_MS = 3_600_000;
const COUNTDOWN_TICK_MS = 60_000;

type Translate = ReturnType<typeof useI18n>["t"];
type Preview = { state: "loading" } | { state: "failed"; error: string } | { state: "ready"; value: VaultDeletionPreview };

/** Days and hours left before the deletion may start, rounded up to the hour. */
function countdown(until: string, now: number, t: Translate): string | null {
  const hours = Math.ceil((Date.parse(until) - now) / HOUR_MS);
  if (!(hours > 0)) return null;
  const days = Math.floor(hours / 24);
  const rest = hours % 24;
  return [days ? t("vault_deletion.days", { count: days }) : "", rest ? t("vault_deletion.hours", { count: rest }) : ""]
    .filter(Boolean).join(" ");
}

function failedLeftovers(failure: Record<string, unknown> | null | undefined): number {
  if (!failure) return 0;
  return ["leftover_versions", "leftover_markers", "leftover_uploads"]
    .reduce((sum, key) => sum + (typeof failure[key] === "number" ? failure[key] : 0), 0);
}

/** What the Owner is about to delete, from a live listing of the whole prefix. */
function PreviewSummary({ preview, t, locale }: { preview: VaultDeletionPreview; t: Translate; locale: string }) {
  const money = (value: number) => formatMoney(value, { currency: preview.currency, locale });
  return <div className="grid gap-2" data-testid="vault-deletion-preview">
    <p>{t("vault_deletion.preview", {
      objects: t("vault_deletion.objects", { count: preview.object_count }),
      versions: t("vault_deletion.versions", { count: preview.version_count }),
      markers: t("vault_deletion.markers", { count: preview.delete_marker_count }),
      bytes: formatBytes(preview.byte_count, locale),
    })}</p>
    {preview.multipart_upload_count > 0 ? <p className="text-sm">{t("vault_deletion.uploads", { count: preview.multipart_upload_count })}</p> : null}
    {preview.by_storage_class.length > 0 ? <div className="overflow-x-auto"><table className="w-full text-sm">
      <thead className="text-left text-xs text-muted"><tr>
        <th scope="col" className="py-1 pr-2">{t("vault_deletion.col_class")}</th>
        <th scope="col" className="py-1 pr-2 text-right">{t("vault_deletion.col_versions")}</th>
        <th scope="col" className="py-1 pr-2 text-right">{t("vault_deletion.col_size")}</th>
        <th scope="col" className="py-1 text-right">{t("vault_deletion.col_early")}</th>
      </tr></thead>
      <tbody>{preview.by_storage_class.map((entry) => <tr key={entry.storage_class} className="border-t border-line">
        <td className="py-1 pr-2 break-all">{entry.storage_class}</td>
        <td className="py-1 pr-2 text-right tabular-nums">{formatGroupedCount(entry.version_count, locale)}</td>
        <td className="py-1 pr-2 text-right tabular-nums">{formatBytes(entry.byte_count, locale)}</td>
        <td className="py-1 text-right tabular-nums">{entry.early_deletion_version_count ? money(entry.early_deletion_cost) : "—"}</td>
      </tr>)}</tbody>
    </table></div> : null}
    <p className="font-semibold" data-testid="vault-deletion-early-cost">{preview.early_deletion_cost > 0
      ? t("vault_deletion.early_cost", { cost: money(preview.early_deletion_cost) })
      : t("vault_deletion.no_early_cost")}</p>
    {/* From the Class Entry of each version (#731); a preview stored before it has no counts. */}
    {preview.early_deletion_version_count !== undefined && preview.early_deletion_byte_count !== undefined ? <MinimumStorageNote t={t} locale={locale}
      kind="versions" testId="vault-deletion-minimum" amount={{
        count: preview.early_deletion_version_count, bytes: preview.early_deletion_byte_count,
        remaining_cost: preview.early_deletion_cost, savings_from: preview.savings_from, currency: preview.currency,
        estimated_count: preview.early_deletion_estimated_count,
        by_storage_class: preview.by_storage_class.filter((entry) => entry.early_deletion_version_count > 0),
      }} /> : null}
    <p className="text-xs text-muted">{t("vault_deletion.early_help")}</p>
    <RetentionLockNote amount={preview.retention_lock} t={t} locale={locale} kind="versions" testId="vault-deletion-retention-lock" />
  </div>;
}

/**
 * Vault Deletion of a Vault Tombstone (#717, ADR-0047): a live preview of the
 * Vault's whole prefix, the exact name, a reason and a fresh reauthentication,
 * then a cancellable seven-day delay; a failure stays visible with a retry.
 */
export function VaultDeletionDialog({ open, onOpenChange, vaultId, vaultName, onCompleted }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vaultId: number;
  vaultName: string;
  /** Runs when the dialog closes after a change, so the outcome stays readable until then. */
  onCompleted?: () => void | Promise<void>;
}) {
  const { t, locale } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const [status, setStatus] = useState<VaultDeletionStatus | null>(null);
  const [deleted, setDeleted] = useState(false);
  const [preview, setPreview] = useState<Preview>({ state: "loading" });
  const [confirmation, setConfirmation] = useState("");
  const [reason, setReason] = useState("");
  const [deleteBucket, setDeleteBucket] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const changed = useRef(false);

  function handleOpenChange(next: boolean) {
    onOpenChange(next);
    if (!next && changed.current) {
      changed.current = false;
      void onCompleted?.();
    }
  }

  const loadPreview = useCallback(async () => {
    setPreview({ state: "loading" });
    try {
      setPreview({ state: "ready", value: await vaultApi(vaultId).previewDeletion() });
    } catch (failure) {
      setPreview({ state: "failed", error: errorMessage(failure, translateRef.current) });
    }
  }, [vaultId]);

  const load = useCallback(async () => {
    setError("");
    try {
      const value = await vaultApi(vaultId).deletion();
      setStatus(value);
      if (!value.request && value.deletable) await loadPreview();
    } catch (failure) {
      // The Vault left the catalog: only its audit record remains.
      if (failure instanceof ApiError && failure.status === 404) {
        setDeleted(true);
        changed.current = true;
      } else setError(errorMessage(failure, translateRef.current));
    }
  }, [vaultId, loadPreview]);

  useEffect(() => {
    if (!open) return;
    setStatus(null);
    setDeleted(false);
    setPreview({ state: "loading" });
    setConfirmation("");
    setReason("");
    setDeleteBucket(false);
    setError("");
    void load();
  }, [open, load]);

  const pendingUntil = status?.request?.status === "pending" ? status.request.pending_until : null;
  useEffect(() => {
    if (!open || !pendingUntil) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), COUNTDOWN_TICK_MS);
    return () => window.clearInterval(timer);
  }, [open, pendingUntil]);

  async function act(action: () => Promise<VaultDeletionStatus>) {
    setBusy(true);
    setError("");
    try {
      setStatus(await action());
      changed.current = true;
    } catch (failure) {
      setError(errorMessage(failure, t));
    } finally { setBusy(false); }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    // Only a dedicated Vault Bucket (empty prefix) can be deleted (#739).
    const dedicated = preview.state === "ready" && preview.value.prefix === "";
    await act(() => vaultApi(vaultId).requestDeletion({ confirmation, reason, delete_bucket: dedicated && deleteBucket }));
  }

  async function cancel() {
    await act(() => vaultApi(vaultId).cancelDeletion());
    // Back to the form, with a fresh listing.
    await loadPreview();
  }

  const request = status?.request ?? null;
  const ready = preview.state === "ready" ? preview.value : null;
  const left = pendingUntil ? countdown(pendingUntil, now, t) : null;
  const leftovers = failedLeftovers(request?.failure);
  const delay = ready ? t("ui.duration_days", { count: Math.round(ready.delay_seconds / 86400) }) : "";

  return <Dialog open={open} onOpenChange={handleOpenChange} title={t("vault_deletion.title")}
    closeLabel={t("ui.close")}
    description={t("vault_deletion.description", { vault: vaultName })}>
    <div className="grid gap-4">
      <p className="font-semibold" data-testid="vault-deletion-local-note">{t("vault_deletion.local_note")}</p>
      {error ? <p role="alert" className="text-destructive">{error}</p> : null}
      {deleted ? <div className="grid gap-3" data-testid="vault-deletion-deleted">
        <p>{t("vault_deletion.deleted")}</p>
        <Button onClick={() => handleOpenChange(false)}>{t("ui.close")}</Button>
      </div> : request ? <div className="grid gap-3" data-testid="vault-deletion-status" data-status={request.status}>
        {request.status === "pending" && request.retained_count ? <div className="grid gap-1" data-testid="vault-deletion-retained">
          {/* Only versions under a Retention Lock are left: waiting, not failed (#736). */}
          <p>{t("vault_deletion.retained", { count: request.retained_count, count_shown: formatGroupedCount(request.retained_count, locale), date: formatUtcDate(request.retained_until, locale) })}</p>
          <p className="text-sm text-muted">{t("vault_deletion.retained_next", { date: formatDateTime(request.next_attempt_at ?? request.pending_until, locale) })}</p>
        </div> : request.status === "pending" ? <>
          <p>{t("vault_deletion.pending", { date: formatDateTime(request.pending_until) })}</p>
          <p data-testid="vault-deletion-countdown">{left ? t("vault_deletion.countdown", { time: left }) : t("vault_deletion.countdown_due")}</p>
        </> : null}
        {request.status === "running" ? <p>{t("vault_deletion.running")}</p> : null}
        {request.preview?.prefix === "" ? <p className="text-sm" data-testid="vault-deletion-bucket">
          {t(request.delete_bucket ? "vault_deletion.bucket_delete" : "vault_deletion.bucket_keep")}
        </p> : null}
        {request.status === "failed" ? <>
          <p role="alert">{t("vault_deletion.failed")}</p>
          {leftovers > 0 ? <p>{t("vault_deletion.failed_leftover", { count: leftovers })}</p> : null}
          {request.failure?.reason === "bucket_identity" ? <p>{t("vault_deletion.failed_bucket_identity")}</p> : null}
          <p className="text-sm text-muted">{t("vault_deletion.failed_help")}</p>
        </> : null}
        {request.preview ? <PreviewSummary preview={request.preview} t={t} locale={locale} /> : null}
        {request.retryable ? <Button variant="danger" disabled={busy} onClick={() => void act(() => vaultApi(vaultId).retryDeletion())}>
          {t("vault_deletion.retry")}
        </Button> : null}
        {request.cancellable ? <Button variant="secondary" disabled={busy} onClick={() => void cancel()}>
          {t("vault_deletion.cancel")}
        </Button> : null}
        <Button variant="secondary" disabled={busy} onClick={() => void load()}>{t("vault_deletion.refresh")}</Button>
        <Button onClick={() => handleOpenChange(false)}>{t("ui.close")}</Button>
      </div> : status ? <form onSubmit={(event) => void submit(event)} className="grid gap-4">
        {status.local_files > 0 ? <p className="text-sm">{t("vault_deletion.local_files", { count: status.local_files })}</p> : null}
        {preview.state === "loading" ? <p className="text-sm text-muted" role="status">{t("vault_deletion.preview_loading")}</p> : null}
        {preview.state === "failed" ? <div className="grid gap-2" data-testid="vault-deletion-preview-failed">
          <p role="alert" className="text-destructive">{t("vault_deletion.preview_failed")}</p>
          {preview.error !== t("vault_deletion.preview_failed") ? <p className="text-sm text-muted">{preview.error}</p> : null}
          <Button variant="secondary" onClick={() => void loadPreview()}>{t("vault_deletion.preview_retry")}</Button>
        </div> : null}
        {ready ? <>
          <p className="text-sm">{t("vault_deletion.scope", { prefix: ready.prefix })}</p>
          <PreviewSummary preview={ready} t={t} locale={locale} />
          <p className="text-sm">{t("vault_deletion.delay", { delay })}</p>
          {ready.prefix === "" ? <div className="grid gap-1" data-testid="vault-deletion-delete-bucket">
            <label className="flex items-center gap-2">
              <input className="size-5 accent-[var(--green)]" type="checkbox" checked={deleteBucket}
                onChange={(event) => setDeleteBucket(event.target.checked)} />
              {t("vault_deletion.delete_bucket")}
            </label>
            <p className="text-xs text-muted">{t("vault_deletion.delete_bucket_help")}</p>
          </div> : null}
        </> : null}
        <FormField label={t("vault_deletion.confirmation", { vault: vaultName })} htmlFor="vault-deletion-confirmation">
          <FormInput id="vault-deletion-confirmation" value={confirmation} onChange={(event) => setConfirmation(event.target.value)}
            autoComplete="off" maxLength={120} required />
        </FormField>
        <FormField label={t("vault_deletion.reason")} htmlFor="vault-deletion-reason">
          <FormInput id="vault-deletion-reason" value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} required />
        </FormField>
        <p className="text-sm text-muted">{t("vault_deletion.reauth")}</p>
        <Button variant="danger" type="submit" disabled={busy || !ready || confirmation !== vaultName || !reason.trim()}>
          {busy ? t("vault_deletion.submitting") : t("vault_deletion.submit")}
        </Button>
      </form> : null}
    </div>
  </Dialog>;
}

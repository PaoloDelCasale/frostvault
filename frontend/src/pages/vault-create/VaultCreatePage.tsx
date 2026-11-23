import { FormEvent, useEffect, useState } from "react";

import { ApiError, browseSourceVolume, createVault, fetchGeneralSourceVolumes, selectVault, vaultApi, type VaultBucketStatus, type VaultCreationCancelled, type VaultCreationMode } from "@/api";

import { AccountPreferencesMenu } from "@/components/AccountPreferencesMenu";
import { FormField, FormInput, FormSelect } from "@/components/FormField";
import { SourceDirectoryBrowser } from "@/components/SourceDirectoryBrowser";
import { VaultCreationCancel } from "@/components/VaultCreationCancel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";
import { errorMessage } from "@/lib/errorMessage";
import { BUCKET_POLL_MS, cancelledMessage, minutesLeft } from "@/lib/vaultBucket";

export type VaultCreatePageProps = {
  displayName: string;
  /** Override navigation (tests inject this via configureApiClient.navigate). */
  onNavigate?: (url: string) => void;
  embedded?: boolean;
};

/** Governance Retention Lock durations offered at creation (#736, ADR-0050). */
const RETENTION_LOCK_CHOICES = [30, 90, 180, 365] as const;
const RETENTION_LOCK_DEFAULT = 180;

function navigateTo(url: string, onNavigate?: (url: string) => void): void {
  if (onNavigate) {
    onNavigate(url);
    return;
  }
  window.location.assign(url);
}

export function VaultCreatePage({ displayName, onNavigate, embedded = false }: VaultCreatePageProps) {
  const Surface = embedded ? "section" : "main";
  const { t } = useI18n();
  const [name, setName] = useState("");
  const [creationMode, setCreationMode] = useState<VaultCreationMode>("empty");
  const [volumeOptions, setVolumeOptions] = useState<string[]>([]);
  const [volumeAlias, setVolumeAlias] = useState("");
  const [relativePath, setRelativePath] = useState<string | null>(null);
  // Optional, chosen only now: it cannot be changed afterwards (#736).
  const [retentionLock, setRetentionLock] = useState(false);
  const [retentionDays, setRetentionDays] = useState<number>(RETENTION_LOCK_DEFAULT);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [volumesFailed, setVolumesFailed] = useState(false);
  const [volumesAttempt, setVolumesAttempt] = useState(0);
  // A Vault saved while its bucket is not ready yet (#735).
  const [pending, setPending] = useState<VaultBucketStatus | null>(null);
  const [pendingName, setPendingName] = useState("");
  const [cancelled, setCancelled] = useState<VaultCreationCancelled | null>(null);

  useEffect(() => {
    let cancelled = false;
    // Any free directory of a healthy, writable Source Volume can be adopted.
    void fetchGeneralSourceVolumes()
      .then((response) => {
        if (cancelled) return;
        const usable = response.items
          .filter((item) => item.health === "ok" && item.access === "rw")
          .map((item) => item.alias)
          .sort();
        setVolumesFailed(false);
        setVolumeOptions(usable);
        if (usable.length > 0) {
          setVolumeAlias(usable[0]);
        }
      })
      .catch(() => {
        if (cancelled) return;
        setVolumeOptions([]);
        setVolumesFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [volumesAttempt]);

  // FrostVault keeps working in the background: follow it until the Vault is active.
  const pendingVault = pending?.vault_id ?? null;
  useEffect(() => {
    if (pendingVault === null) return;
    let stopped = false;
    const timer = window.setInterval(() => {
      void vaultApi(pendingVault).bucket().then(async (state) => {
        if (stopped) return;
        if (state.state === "ready") {
          stopped = true;
          await openVault(state.vault_id);
        } else setPending(state);
      }).catch(() => undefined);
    }, BUCKET_POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
    // openVault only navigates; the poll restarts only for another Vault.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingVault]);

  const adoptAvailable = volumeOptions.length > 0;

  function failureText(err: unknown): string {
    return err instanceof ApiError || err instanceof TypeError
      ? errorMessage(err, t)
      : (err instanceof Error && err.message) || t("ui.vault_create.failed");
  }

  async function openVault(vaultId: number) {
    await selectVault({ vault_id: vaultId });
    navigateTo(`/?vault_id=${vaultId}`, onNavigate);
  }

  async function handleRetry() {
    if (!pending) return;
    setError(null);
    setSubmitting(true);
    try {
      const state = await vaultApi(pending.vault_id).retryBucket();
      if (state.state === "ready") await openVault(state.vault_id);
      else setPending(state);
    } catch (err) {
      setError(failureText(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (creationMode === "adopt") {
      if (!volumeAlias || relativePath === null) {
        setError(t("ui.vault_create.adopt_path_required"));
        return;
      }
    }
    setSubmitting(true);
    try {
      const payload: {
        name: string;
        creation_mode: VaultCreationMode;
        volume_alias?: string;
        relative_path?: string;
        retention_lock_days?: 30 | 90 | 180 | 365 | null;
      } = {
        name: name.trim(),
        creation_mode: creationMode,
      };
      if (retentionLock) payload.retention_lock_days = retentionDays as 30 | 90 | 180 | 365;
      if (creationMode === "adopt") {
        payload.volume_alias = volumeAlias;
        payload.relative_path = relativePath ?? "";
      }

      const vault = await createVault(payload);
      // Active only once its bucket is created and verified; otherwise the
      // Vault is saved and FrostVault keeps retrying in the background.
      if (vault.lifecycle === "active") await openVault(vault.id);
      else {
        setPendingName(vault.name);
        setPending(vault.bucket);
      }
    } catch (err) {
      setError(failureText(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Surface className={`relative bg-canvas ${embedded ? "" : "min-h-screen px-4 py-[30px]"}`}>
      {!embedded && <div className="absolute top-[max(0.75rem,env(safe-area-inset-top))] right-[max(1rem,env(safe-area-inset-right))]">
        <AccountPreferencesMenu />
      </div>}
      <div className="grid w-full max-w-[min(560px,100%)] gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold">{t("ui.vault_create.title")}</h1>
          <p className="text-sm text-muted">{t("ui.vault_create.subtitle", { name: displayName })}</p>
        </div>
        <div className="rounded-panel border border-line bg-surface p-4">
          {cancelled ? (
            <div className="grid gap-3.5">
              <p className="m-0 rounded-[10px] border border-line bg-canvas px-3.5 py-3 text-sm break-words text-ink" role="status" data-testid="vault-create-cancelled">
                {cancelledMessage(cancelled, t)}
              </p>
              <div className="flex flex-wrap items-center gap-2.5">
                <Button type="button" variant="primary" onClick={() => navigateTo("/", onNavigate)}>
                  {t("ui.vault_create.open_overview")}
                </Button>
              </div>
            </div>
          ) : pending ? (
            <div className="grid gap-3.5">
              {pending.state === "failed" ? (
                <div className="grid gap-2 rounded-[10px] bg-red-soft px-3.5 py-3 text-sm text-ink" role="alert" data-testid="vault-create-bucket-failed">
                  <p className="m-0 font-bold">{t("ui.vault_create.bucket_failed_title")}</p>
                  <p className="m-0 break-words">{t("ui.vault_create.bucket_failed", { bucket: pending.bucket, error: pending.error ?? "" })}</p>
                  {pending.stage ? <p className="m-0 text-muted">{t(`vault_bucket.stage_${pending.stage}`)}</p> : null}
                  <p className="m-0">{t("ui.vault_create.bucket_failed_help")}</p>
                </div>
              ) : pending.state === "settling" ? (
                <div className="grid gap-2 rounded-[10px] border border-line bg-canvas px-3.5 py-3 text-sm text-ink" role="status" data-testid="vault-create-bucket-settling">
                  <p className="m-0 font-bold">{t("ui.vault_create.bucket_settling_title")}</p>
                  <p className="m-0 break-words">{t("ui.vault_create.bucket_settling", { bucket: pending.bucket, count: minutesLeft(pending) })}</p>
                </div>
              ) : (
                // Creating, no error yet: progress, never an empty failure (#735).
                <div className="grid gap-2 rounded-[10px] border border-line bg-canvas px-3.5 py-3 text-sm text-ink" role="status" data-testid="vault-create-bucket-creating">
                  <p className="m-0 font-bold">{t("ui.vault_create.bucket_creating_title")}</p>
                  <p className="m-0 break-words">{t("ui.vault_create.bucket_creating", { bucket: pending.bucket })}</p>
                  {pending.stage ? <p className="m-0 text-muted">{t(`vault_bucket.stage_${pending.stage}`)}</p> : null}
                </div>
              )}
              {submitting ? <p className="m-0 text-sm text-muted" role="status">{t("ui.vault_create.creating_bucket")}</p> : null}
              {error ? <div className="rounded-[10px] bg-red-soft px-3.5 py-3 text-sm text-ink" role="alert">{error}</div> : null}
              <div className="flex flex-wrap items-center gap-2.5">
                {pending.state !== "settling" ? (
                  <Button type="button" variant="primary" disabled={submitting} onClick={() => void handleRetry()}>
                    {t("ui.vault_create.bucket_retry")}
                  </Button>
                ) : null}
                <Button type="button" variant="secondary" onClick={() => navigateTo("/", onNavigate)}>
                  {t("ui.vault_create.open_overview")}
                </Button>
                <VaultCreationCancel vaultId={pending.vault_id} vaultName={pendingName} bucket={pending.bucket}
                  disabled={submitting} onCancelled={(outcome) => { setPending(null); setCancelled(outcome); }} />
              </div>
            </div>
          ) : (
            <form className="grid gap-3.5" onSubmit={(e) => void handleSubmit(e)}>
              <FormField label={t("ui.vault_create.name")} htmlFor="vault-name">
                <FormInput
                  id="vault-name"
                  name="name"
                  maxLength={120}
                  autoComplete="off"
                  required
                  autoFocus
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </FormField>

              {adoptAvailable ? (
                <fieldset className="grid gap-2 border-0 p-0">
                  <legend className="text-[13px] font-bold text-muted">
                    {t("ui.vault_create.creation_mode")}
                  </legend>
                  <label className="flex min-h-11 items-center gap-3 text-sm text-ink">
                    <input
                      type="radio"
                      name="creation_mode"
                      value="empty"
                      checked={creationMode === "empty"}
                      onChange={() => {
                        setCreationMode("empty");
                        setRelativePath(null);
                      }}
                      className="size-4 accent-green"
                    />
                    {t("ui.vault_create.mode_empty")}
                  </label>
                  <label className="flex min-h-11 items-center gap-3 text-sm text-ink">
                    <input
                      type="radio"
                      name="creation_mode"
                      value="adopt"
                      checked={creationMode === "adopt"}
                      onChange={() => setCreationMode("adopt")}
                      className="size-4 accent-green"
                    />
                    {t("ui.vault_create.mode_adopt")}
                  </label>
                </fieldset>
              ) : null}

              {volumesFailed ? (
                <div className="grid gap-2 rounded-[10px] border border-line bg-canvas px-3.5 py-3 text-sm text-ink" role="status" data-testid="vault-create-volumes-error">
                  <p className="m-0">{t("ui.vault_create.volumes_failed")}</p>
                  <div>
                    <Button type="button" variant="secondary" onClick={() => setVolumesAttempt((value) => value + 1)}>
                      {t("ui.retry")}
                    </Button>
                  </div>
                </div>
              ) : null}

              {creationMode === "adopt" && adoptAvailable ? (
                <div className="grid gap-3" data-testid="vault-create-adopt">
                  <FormField
                    label={t("ui.vault_create.volume")}
                    htmlFor="vault-volume"
                  >
                    <FormSelect
                      id="vault-volume"
                      value={volumeAlias}
                      onChange={(e) => {
                        setVolumeAlias(e.target.value);
                        setRelativePath(null);
                      }}
                    >
                      {volumeOptions.map((alias) => (
                        <option key={alias} value={alias}>
                          {alias}
                        </option>
                      ))}
                    </FormSelect>
                  </FormField>
                  {volumeAlias ? (
                    <SourceDirectoryBrowser
                      volumeAlias={volumeAlias}
                      browse={browseSourceVolume}
                      selectedPath={relativePath}
                      onSelect={setRelativePath}
                    />
                  ) : null}
                </div>
              ) : null}

              <fieldset className="grid gap-2 rounded-[10px] border border-line bg-canvas px-3.5 py-3" data-testid="vault-create-retention-lock">
                <legend className="px-1 text-[13px] font-bold text-muted">{t("ui.vault_create.retention_lock")}</legend>
                <label className="flex min-h-11 items-center gap-3 text-sm font-semibold text-ink">
                  <input
                    type="checkbox"
                    name="retention_lock"
                    checked={retentionLock}
                    onChange={(e) => setRetentionLock(e.target.checked)}
                    className="size-4 accent-green"
                  />
                  {t("ui.vault_create.retention_lock_enable")}
                </label>
                <p className="m-0 text-sm text-muted">{t("ui.vault_create.retention_lock_help")}</p>
                {retentionLock ? (
                  <>
                    <FormField label={t("ui.vault_create.retention_lock_days")} htmlFor="vault-retention-days">
                      <FormSelect
                        id="vault-retention-days"
                        value={String(retentionDays)}
                        onChange={(e) => setRetentionDays(Number(e.target.value))}
                      >
                        {RETENTION_LOCK_CHOICES.map((days) => (
                          <option key={days} value={days}>
                            {t(days === RETENTION_LOCK_DEFAULT ? "ui.vault_create.retention_lock_option_default" : "ui.vault_create.retention_lock_option", { count: days })}
                          </option>
                        ))}
                      </FormSelect>
                    </FormField>
                    <p className="m-0 text-sm text-ink" data-testid="vault-create-retention-lock-consequences">{t("ui.vault_create.retention_lock_consequences", { count: retentionDays })}</p>
                  </>
                ) : null}
              </fieldset>

              {error ? (
                <div
                  className="rounded-[10px] bg-red-soft px-3.5 py-3 text-sm text-ink"
                  role="alert"
                  aria-live="assertive"
                >
                  {error}
                </div>
              ) : null}

              <p className="m-0 text-sm text-muted">{t("ui.vault_create.bucket_hint")}</p>
              {submitting ? <p className="m-0 text-sm text-muted" role="status" data-testid="vault-create-progress">{t("ui.vault_create.creating_bucket")}</p> : null}

              <div className="flex flex-wrap items-center gap-2.5">
                <Button type="submit" variant="primary" disabled={submitting}>
                  {t("ui.vault_create.submit")}
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => navigateTo("/", onNavigate)}
                >
                  {t("ui.vault_create.cancel")}
                </Button>
              </div>
            </form>
          )}
        </div>
      </div>

    </Surface>
  );
}

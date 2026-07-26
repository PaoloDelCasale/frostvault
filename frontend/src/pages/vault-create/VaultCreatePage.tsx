import { FormEvent, useState } from "react";

import {
  ApiError,
  confirmRecoveryCustody,
  createVault,
  selectVault,
} from "@/api";
import type { EncryptionMode, VaultCreateResponse } from "@/api";
import { AuthCard } from "@/components/AuthCard";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { FormField, FormInput } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

import { RecoveryExportPanel } from "./RecoveryExportPanel";

export type VaultCreatePageProps = {
  displayName: string;
  /** Override navigation (tests inject this via configureApiClient.navigate). */
  onNavigate?: (url: string) => void;
};

function navigateTo(url: string, onNavigate?: (url: string) => void): void {
  if (onNavigate) {
    onNavigate(url);
    return;
  }
  window.location.assign(url);
}

export function VaultCreatePage({ displayName, onNavigate }: VaultCreatePageProps) {
  const { t } = useI18n();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [encryptionMode, setEncryptionMode] = useState<EncryptionMode>("plain");
  const [error, setError] = useState<string | null>(null);
  const [recoveryError, setRecoveryError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [createdVault, setCreatedVault] = useState<VaultCreateResponse | null>(null);
  const [custodyConfirmed, setCustodyConfirmed] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const payload: {
        name: string;
        encryption_mode: EncryptionMode;
        slug?: string;
      } = {
        name: name.trim(),
        encryption_mode: encryptionMode,
      };
      const trimmedSlug = slug.trim();
      if (trimmedSlug) payload.slug = trimmedSlug;

      const vault = await createVault(payload);
      if (vault.encryption_mode === "crypt" && vault.recovery_export) {
        setCreatedVault(vault);
        setCustodyConfirmed(false);
        return;
      }
      await selectVault({ vault_id: vault.id });
      navigateTo("/", onNavigate);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : t("ui.vault_create.failed");
      setError(message || t("ui.vault_create.failed"));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleConfirmCustody() {
    if (!createdVault) return;
    setRecoveryError(null);
    setConfirming(true);
    try {
      await selectVault({ vault_id: createdVault.id });
      await confirmRecoveryCustody({ acknowledged: true });
      setCustodyConfirmed(true);
      navigateTo("/", onNavigate);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : t("ui.recovery.confirm_failed");
      setRecoveryError(message || t("ui.recovery.confirm_failed"));
    } finally {
      setConfirming(false);
      setConfirmOpen(false);
    }
  }

  const recoveryExport = createdVault?.recovery_export;

  return (
    <main className="grid min-h-screen place-items-center bg-canvas px-4 py-[30px]">
      <div className="w-full max-w-[min(440px,100%)]">
        <AuthCard>
          <p className="m-0 mb-1.5 text-[12px] font-extrabold tracking-[0.16em] text-green">
            {t("ui.private_archive")}
          </p>
          <h1 className="m-0 text-[clamp(1.75rem,5vw,2.25rem)] font-bold tracking-[-0.04em] text-ink">
            {t("ui.vault_create.title")}
          </h1>
          <p className="mt-2 text-sm text-muted">
            {t("ui.vault_create.subtitle", { name: displayName })}
          </p>

          {recoveryExport ? (
            <RecoveryExportPanel
              recoveryExport={recoveryExport}
              title={t("ui.recovery.title")}
              subtitle={t("ui.recovery.subtitle")}
              exportLabel={t("ui.recovery.export_label")}
              warning={t("ui.recovery.warning")}
              copyLabel={t("ui.recovery.copy")}
              downloadLabel={t("ui.recovery.download")}
            >
              {recoveryError ? (
                <div
                  className="rounded-[10px] bg-red-soft px-3.5 py-3 text-sm text-ink"
                  role="alert"
                  aria-live="assertive"
                >
                  {recoveryError}
                </div>
              ) : null}
              {!custodyConfirmed ? (
                <div className="flex flex-wrap gap-2.5">
                  <Button
                    type="button"
                    variant="primary"
                    disabled={confirming}
                    onClick={() => setConfirmOpen(true)}
                  >
                    {t("ui.recovery.confirm")}
                  </Button>
                </div>
              ) : null}
            </RecoveryExportPanel>
          ) : (
            <form className="mt-6 grid gap-3.5" onSubmit={(e) => void handleSubmit(e)}>
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

              <FormField
                label={`${t("ui.vault_create.slug")} ${t("ui.vault_create.slug_optional")}`}
                htmlFor="vault-slug"
                help={t("ui.vault_create.slug_placeholder")}
              >
                <FormInput
                  id="vault-slug"
                  name="slug"
                  maxLength={60}
                  pattern="[a-z0-9-]+"
                  autoComplete="off"
                  placeholder={t("ui.vault_create.slug_placeholder")}
                  value={slug}
                  onChange={(e) => setSlug(e.target.value)}
                />
              </FormField>

              <fieldset className="grid gap-2 border-0 p-0">
                <legend className="text-[13px] font-bold text-muted">
                  {t("ui.vault_create.encryption")}
                </legend>
                <label className="flex min-h-11 items-center gap-3 text-sm text-ink">
                  <input
                    type="radio"
                    name="encryption_mode"
                    value="plain"
                    checked={encryptionMode === "plain"}
                    onChange={() => setEncryptionMode("plain")}
                    className="size-4 accent-green"
                  />
                  {t("ui.vault_create.encryption_plain")}
                </label>
                <label className="flex min-h-11 items-center gap-3 text-sm text-ink">
                  <input
                    type="radio"
                    name="encryption_mode"
                    value="crypt"
                    checked={encryptionMode === "crypt"}
                    onChange={() => setEncryptionMode("crypt")}
                    className="size-4 accent-green"
                  />
                  {t("ui.vault_create.encryption_crypt")}
                </label>
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
        </AuthCard>
      </div>

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={t("ui.recovery.confirm_title")}
        description={t("ui.recovery.confirm_description")}
        confirmLabel={t("ui.recovery.confirm_action")}
        cancelLabel={t("ui.vault_create.cancel")}
        tone="danger"
        onConfirm={() => {
          void handleConfirmCustody();
        }}
      />
    </main>
  );
}

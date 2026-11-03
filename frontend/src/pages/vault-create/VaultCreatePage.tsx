import { FormEvent, useEffect, useState } from "react";

import {
  ApiError,
  browseSourceVolume,
  createVault,
  fetchAdminSourceVolumes,
  selectVault,
} from "@/api";
import type {
  VaultCreationMode,
} from "@/api";

import { AccountPreferencesMenu } from "@/components/AccountPreferencesMenu";
import { AuthCard } from "@/components/AuthCard";
import { FormField, FormInput, FormSelect } from "@/components/FormField";
import { SourceDirectoryBrowser } from "@/components/SourceDirectoryBrowser";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

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
  const [creationMode, setCreationMode] = useState<VaultCreationMode>("empty");
  const [volumeOptions, setVolumeOptions] = useState<string[]>([]);
  const [volumeAlias, setVolumeAlias] = useState("");
  const [relativePath, setRelativePath] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // Any free directory of a healthy, writable Source Volume can be adopted.
    void fetchAdminSourceVolumes()
      .then((response) => {
        if (cancelled) return;
        const usable = response.items
          .filter((item) => item.health === "ok" && item.access === "rw")
          .map((item) => item.alias)
          .sort();
        setVolumeOptions(usable);
        if (usable.length > 0) {
          setVolumeAlias(usable[0]);
        }
      })
      .catch(() => {
        if (!cancelled) setVolumeOptions([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const adoptAvailable = volumeOptions.length > 0;

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
        slug?: string;
        volume_alias?: string;
        relative_path?: string;
      } = {
        name: name.trim(),
        creation_mode: creationMode,
      };
      const trimmedSlug = slug.trim();
      if (trimmedSlug) payload.slug = trimmedSlug;
      if (creationMode === "adopt") {
        payload.volume_alias = volumeAlias;
        payload.relative_path = relativePath ?? "";
      }

      const vault = await createVault(payload);
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

  return (
    <main className="relative grid min-h-screen place-items-center bg-canvas px-4 py-[30px]">
      <div className="absolute top-[max(0.75rem,env(safe-area-inset-top))] right-[max(1rem,env(safe-area-inset-right))]">
        <AccountPreferencesMenu />
      </div>
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
        </AuthCard>
      </div>

    </main>
  );
}

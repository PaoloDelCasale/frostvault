import { FormEvent, useEffect, useState } from "react";

import {
  ApiError,
  browseSourceVolume,
  createVault,
  fetchGeneralSourceVolumes,
  selectVault,
} from "@/api";
import type {
  VaultCreationMode,
} from "@/api";

import { AccountPreferencesMenu } from "@/components/AccountPreferencesMenu";
import { FormField, FormInput, FormSelect } from "@/components/FormField";
import { SourceDirectoryBrowser } from "@/components/SourceDirectoryBrowser";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";
import { errorMessage } from "@/lib/errorMessage";

export type VaultCreatePageProps = {
  displayName: string;
  /** Override navigation (tests inject this via configureApiClient.navigate). */
  onNavigate?: (url: string) => void;
  embedded?: boolean;
};

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
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [volumesFailed, setVolumesFailed] = useState(false);
  const [volumesAttempt, setVolumesAttempt] = useState(0);

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
        volume_alias?: string;
        relative_path?: string;
      } = {
        name: name.trim(),
        creation_mode: creationMode,
      };
      if (creationMode === "adopt") {
        payload.volume_alias = volumeAlias;
        payload.relative_path = relativePath ?? "";
      }

      const vault = await createVault(payload);
      await selectVault({ vault_id: vault.id });
      navigateTo(`/?vault_id=${vault.id}`, onNavigate);
    } catch (err) {
      setError(
        err instanceof ApiError || err instanceof TypeError
          ? errorMessage(err, t)
          : (err instanceof Error && err.message) || t("ui.vault_create.failed"),
      );
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
        </div>
      </div>

    </Surface>
  );
}

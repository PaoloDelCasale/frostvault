import { useEffect, useState } from "react";

import {
  deleteLifecycleFolderOverride,
  fetchLifecycle,
  updateLifecycleDefault,
  upsertLifecycleFolderOverride,
  type LifecycleGuidedProfile,
  type LifecycleResponse,
} from "@/api";
import { FormField, FormInput, FormSelect } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

type LifecyclePanelProps = {
  onNotice: (message: string, error?: boolean) => void;
};

function profileLabel(
  name: string,
  profile: LifecycleGuidedProfile | undefined,
  t: (key: string, params?: Record<string, unknown>) => string,
): string {
  if (!profile?.transitions?.length) {
    return t("access.lifecycle_keep_standard", { name });
  }
  const steps = profile.transitions
    .map((step) => `${step.storage_class} @ ${step.days}d`)
    .join(" → ");
  return t("access.lifecycle_profile_steps", { name, steps });
}

function selectedDefaultProfile(data: LifecycleResponse): string {
  const guided = data.guided_profiles || {};
  const defaultPolicy = (data.policies || []).find(
    (item) => item.id === data.default_policy_id,
  );
  let selected = "standard_only";
  if (defaultPolicy?.profile) {
    const transitions = JSON.stringify(defaultPolicy.profile.transitions || []);
    for (const [name, profile] of Object.entries(guided)) {
      if (JSON.stringify(profile.transitions || []) === transitions) {
        selected = name;
        break;
      }
    }
  }
  return selected in guided ? selected : Object.keys(guided)[0] ?? "standard_only";
}

export function LifecyclePanel({ onNotice }: LifecyclePanelProps) {
  const { t, ready } = useI18n();
  const [loadState, setLoadState] = useState("");
  const [data, setData] = useState<LifecycleResponse | null>(null);
  const [defaultProfile, setDefaultProfile] = useState("standard_only");
  const [folderPath, setFolderPath] = useState("");
  const [folderProfile, setFolderProfile] = useState("archive_tiered");
  const [warnings, setWarnings] = useState("");
  const [busy, setBusy] = useState(false);

  function applyLifecycle(next: LifecycleResponse) {
    setData(next);
    setDefaultProfile(selectedDefaultProfile(next));
    const guided = next.guided_profiles || {};
    setFolderProfile(
      "archive_tiered" in guided
        ? "archive_tiered"
        : Object.keys(guided)[0] ?? "standard_only",
    );
    setWarnings((next.warnings || []).join(" "));
    setLoadState(t("access.lifecycle_loaded"));
  }

  useEffect(() => {
    if (!ready) return;
    setLoadState(t("access.lifecycle_loading"));
    void (async () => {
      try {
        applyLifecycle(await fetchLifecycle());
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setLoadState(message);
        onNotice(message, true);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load when i18n ready
  }, [ready]);

  const profiles = Object.entries(data?.guided_profiles ?? {});

  async function onSaveDefault(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const next = await updateLifecycleDefault(defaultProfile);
      applyLifecycle(next);
      onNotice(warnings || t("api.lifecycle_profile_updated"));
    } catch (error) {
      onNotice(error instanceof Error ? error.message : String(error), true);
    } finally {
      setBusy(false);
    }
  }

  async function onSaveOverride(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const next = await upsertLifecycleFolderOverride(folderPath, folderProfile);
      applyLifecycle(next);
      setFolderPath("");
      onNotice((next.warnings || []).join(" ") || t("api.lifecycle_override_updated"));
    } catch (error) {
      onNotice(error instanceof Error ? error.message : String(error), true);
    } finally {
      setBusy(false);
    }
  }

  async function onRemoveOverride(path: string) {
    setBusy(true);
    try {
      const next = await deleteLifecycleFolderOverride(path);
      applyLifecycle(next);
      onNotice(t("api.lifecycle_override_removed"));
    } catch (error) {
      onNotice(error instanceof Error ? error.message : String(error), true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section data-panel="lifecycle">
      <Panel className="p-4 sm:p-5">
        <h2 className="text-lg font-bold">{t("access.lifecycle_title")}</h2>
        <p className="mt-1 text-sm text-muted">{t("access.lifecycle_help")}</p>
        <p className="mt-2 text-sm text-muted" role="status">
          {loadState}
        </p>

        <form className="mt-4 grid gap-3" onSubmit={(event) => void onSaveDefault(event)}>
          <FormField label={t("access.lifecycle_default")} htmlFor="lifecycle-default-profile">
            <FormSelect
              id="lifecycle-default-profile"
              value={defaultProfile}
              onChange={(event) => setDefaultProfile(event.target.value)}
              required
            >
              {profiles.map(([name, profile]) => (
                <option key={name} value={name}>
                  {profileLabel(name, profile, t)}
                </option>
              ))}
            </FormSelect>
          </FormField>
          <Button type="submit" className="min-h-11 w-full sm:w-auto" disabled={busy || !data}>
            {t("access.lifecycle_save_default")}
          </Button>
        </form>

        <form className="mt-6 grid gap-3 border-t border-line pt-4" onSubmit={(event) => void onSaveOverride(event)}>
          <h3 className="text-sm font-bold">{t("access.lifecycle_override")}</h3>
          <FormField label={t("access.lifecycle_folder_path")} htmlFor="lifecycle-folder-path">
            <FormInput
              id="lifecycle-folder-path"
              maxLength={500}
              required
              value={folderPath}
              onChange={(event) => setFolderPath(event.target.value)}
              placeholder="photos/2024"
            />
          </FormField>
          <FormField label={t("access.lifecycle_folder_profile")} htmlFor="lifecycle-folder-profile">
            <FormSelect
              id="lifecycle-folder-profile"
              value={folderProfile}
              onChange={(event) => setFolderProfile(event.target.value)}
              required
            >
              {profiles.map(([name, profile]) => (
                <option key={name} value={name}>
                  {profileLabel(name, profile, t)}
                </option>
              ))}
            </FormSelect>
          </FormField>
          <Button type="submit" className="min-h-11 w-full sm:w-auto" disabled={busy || !data}>
            {t("access.lifecycle_save_override")}
          </Button>
        </form>

        <div className="mt-4 grid gap-2">
          {(data?.folder_overrides ?? []).length === 0 ? (
            <p className="text-sm text-muted">{t("access.lifecycle_no_overrides")}</p>
          ) : (
            (data?.folder_overrides ?? []).map((item) => {
              const policy = (data?.policies ?? []).find(
                (row) => row.id === item.policy_id,
              );
              const label = policy?.name ?? String(item.policy_id);
              return (
                <div
                  key={item.folder_path}
                  className="flex flex-col gap-2 rounded-[14px] border border-line bg-canvas p-3 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div>
                    <strong className="block">{item.folder_path}</strong>
                    <small className="text-muted">{label}</small>
                  </div>
                  <Button
                    type="button"
                    variant="secondary"
                    className="min-h-11 w-full sm:w-auto"
                    disabled={busy}
                    onClick={() => void onRemoveOverride(item.folder_path)}
                  >
                    {t("access.lifecycle_remove_override")}
                  </Button>
                </div>
              );
            })
          )}
        </div>
        {warnings ? (
          <p className="mt-3 text-sm text-muted" role="status">
            {warnings}
          </p>
        ) : null}
      </Panel>
    </section>
  );
}

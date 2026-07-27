import { useEffect, useState } from "react";

import {
  fetchSystemSettings,
  updateSystemSettings,
  type SystemSettingItem,
  type SystemSettingsResponse,
  type SystemSettingValue,
} from "@/api";
import { Badge } from "@/components/Badge";
import { FormInput, FormSelect } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

export type SettingsSectionMode = "defaults" | "deployment";

function SettingValue({
  item,
  value,
  onChange,
  trueLabel,
  falseLabel,
  configuredLabel,
  notConfiguredLabel,
}: {
  item: SystemSettingItem;
  value: SystemSettingValue;
  onChange: (value: string) => void;
  trueLabel: string;
  falseLabel: string;
  configuredLabel: string;
  notConfiguredLabel: string;
}) {
  if ("configured" in item) {
    return (
      <Badge
        state={item.configured ? "both" : "missing"}
        label={item.configured ? configuredLabel : notConfiguredLabel}
      />
    );
  }

  if (typeof item.effective_value === "boolean") {
    return (
      <FormSelect
        aria-label={item.environment_variable}
        value={String(value)}
        disabled={item.mutability !== "runtime_managed"}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="true">{trueLabel}</option>
        <option value="false">{falseLabel}</option>
      </FormSelect>
    );
  }
  if (item.choices?.length) {
    return (
      <FormSelect
        aria-label={item.environment_variable}
        value={String(value ?? "")}
        disabled={item.mutability !== "runtime_managed"}
        onChange={(event) => onChange(event.target.value)}
      >
        {item.choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </FormSelect>
    );
  }
  return (
    <FormInput
      aria-label={item.environment_variable}
      type={typeof value === "number" ? "number" : "text"}
      value={value === null || value === undefined ? "" : String(value)}
      min={item.minimum}
      max={item.maximum}
      disabled={item.mutability !== "runtime_managed"}
      readOnly={item.mutability !== "runtime_managed"}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

export function SettingsSection({ mode }: { mode: SettingsSectionMode }) {
  const { t } = useI18n();
  const [settings, setSettings] = useState<SystemSettingsResponse | null>(null);
  const [draftValues, setDraftValues] = useState<Record<string, SystemSettingValue>>({});
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void fetchSystemSettings()
      .then((response) => {
        if (!cancelled) {
          setSettings(response);
          setDraftValues(
            Object.fromEntries(
              Object.values(response.groups)
                .flat()
                .filter((item) => "effective_value" in item)
                .map((item) => [item.key, item.effective_value ?? null]),
            ),
          );
        }
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <p role="alert">{error}</p>;
  if (!settings) return <p className="text-sm text-muted">{t("admin.settings_loading")}</p>;

  async function restoreDefault(key: string) {
    if (!settings) return;
    setSaving(true);
    setError("");
    try {
      const response = await updateSystemSettings({
        revision: settings.revision,
        overrides: {},
        removals: [key],
      });
      setSettings(response);
      setDraftValues(
        Object.fromEntries(
          Object.values(response.groups)
            .flat()
            .filter((item) => "effective_value" in item)
            .map((item) => [item.key, item.effective_value ?? null]),
        ),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  }

  async function saveDefaults() {
    if (!settings) return;
    const runtimeItems = Object.values(settings.groups)
      .flat()
      .filter((item) => item.mutability === "runtime_managed");
    const overrides = Object.fromEntries(
      runtimeItems
        .filter((item) => String(draftValues[item.key] ?? "") !== String(item.effective_value ?? ""))
        .map((item) => {
          const draft = draftValues[item.key];
          const value = typeof item.effective_value === "number"
            ? Number(draft)
            : typeof item.effective_value === "boolean"
              ? draft === true || draft === "true"
              : draft;
          return [item.key, value];
        }),
    );
    setSaving(true);
    setError("");
    try {
      const response = await updateSystemSettings({
        revision: settings.revision,
        overrides,
        removals: [],
      });
      setSettings(response);
      setDraftValues(
        Object.fromEntries(
          Object.values(response.groups)
            .flat()
            .filter((item) => "effective_value" in item)
            .map((item) => [item.key, item.effective_value ?? null]),
        ),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  }

  const groups = Object.entries(settings.groups)
    .map(([name, items]) => [
      name,
      items.filter((item) =>
        mode === "defaults"
          ? item.mutability === "runtime_managed"
          : item.mutability !== "runtime_managed",
      ),
    ] as const)
    .filter(([, items]) => items.length > 0);

  return (
    <section aria-labelledby={`admin-${mode}-heading`} className="grid gap-4">
      <div>
        <h2 id={`admin-${mode}-heading`} className="text-xl font-bold">
          {mode === "defaults"
            ? t("admin.defaults_heading")
            : t("admin.deployment_heading")}
        </h2>
        <p className="mt-1 text-sm text-muted">
          {mode === "defaults"
            ? t("admin.defaults_help")
            : t("admin.deployment_help")}
        </p>
      </div>
      {groups.map(([group, items]) => (
        <Panel key={group} className="p-5">
          <h3 className="mb-3 text-lg font-bold">{t(`admin.settings_group_${group}`)}</h3>
          <ul className="grid gap-4">
            {items.map((item) => (
              <li key={item.key} className="grid gap-2 border-b border-line pb-4 last:border-0 last:pb-0">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <strong className="block">{item.environment_variable}</strong>
                    <code className="text-xs text-muted">{item.key}</code>
                  </div>
                  <span className="text-xs capitalize text-muted">
                    {t(`admin.settings_source_${item.source}`)} · {item.restart_required
                      ? t("admin.restart_required")
                      : t("admin.applies_immediately")}
                  </span>
                </div>
                <div className="flex flex-wrap items-end gap-2">
                  <div className="min-w-48 flex-1">
                    <SettingValue
                      item={item}
                      value={draftValues[item.key] ?? item.effective_value ?? null}
                      onChange={(value) =>
                        setDraftValues((current) => ({ ...current, [item.key]: value }))
                      }
                      trueLabel={t("admin.boolean_true")}
                      falseLabel={t("admin.boolean_false")}
                      configuredLabel={t("admin.configured")}
                      notConfiguredLabel={t("admin.not_configured")}
                    />
                  </div>
                  {item.mutability === "runtime_managed" && item.source === "database_override" ? (
                    <Button type="button" variant="secondary" disabled={saving} onClick={() => void restoreDefault(item.key)}>
                      {t("admin.restore_default")}
                    </Button>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        </Panel>
      ))}
      {mode === "defaults" ? (
        <div className="flex justify-end">
          <Button type="button" variant="primary" disabled={saving} onClick={() => void saveDefaults()}>
            {t("admin.defaults_save")}
          </Button>
        </div>
      ) : null}
    </section>
  );
}

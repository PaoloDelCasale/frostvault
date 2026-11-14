import { useEffect, useState } from "react";

import {
  fetchOperationPolicy,
  updateOperationPolicy,
  type OperationPolicy,
} from "@/api";
import { FormField, FormSelect } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

type RetentionPanelProps = {
  vaultId: number;
  onNotice: (message: string, error?: boolean) => void;
};
type ReleasePeriod = NonNullable<OperationPolicy["local_release_after_days"]>;
const periods: ReleasePeriod[] = [90, 180, 365, 730];

export function RetentionPanel({ onNotice, vaultId }: RetentionPanelProps) {
  const { t, ready } = useI18n();
  const [loadState, setLoadState] = useState("");
  const [loadError, setLoadError] = useState("");
  const [policy, setPolicy] = useState<OperationPolicy | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [days, setDays] = useState<ReleasePeriod>(180);
  const [saving, setSaving] = useState(false);

  function acceptPolicy(data: OperationPolicy) {
    setPolicy(data);
    setEnabled(data.local_release_after_days != null);
    setDays(data.local_release_after_days ?? 180);
  }

  useEffect(() => {
    if (!ready) return;
    setLoadState(t("folder_settings.retention_loading"));
    setLoadError("");
    void (async () => {
      try {
        acceptPolicy(await fetchOperationPolicy(vaultId));
        setLoadState(t("folder_settings.retention_loaded"));
      } catch (error) {
        setLoadState("");
        setLoadError(error instanceof Error ? error.message : String(error));
      }
    })();
  }, [ready, t, vaultId]);

  async function onSave(event: React.FormEvent) {
    event.preventDefault();
    if (!policy) return;
    setSaving(true);
    try {
      acceptPolicy(await updateOperationPolicy({
        local_release_after_days: enabled ? days : null,
      }, vaultId));
      onNotice(t(enabled ? "folder_settings.retention_enabled" : "folder_settings.retention_disabled"));
    } catch (error) {
      onNotice(error instanceof Error ? error.message : String(error), true);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section data-panel="retention">
      <div>
        <p className="sr-only" role="status">{loadState}</p>
        {loadError ? <p role="alert" className="mb-3 text-sm text-destructive">{loadError}</p> : null}
        <form className="grid gap-3" onSubmit={(event) => void onSave(event)}>
          <label className="flex min-h-11 items-center gap-3 text-sm font-bold text-ink">
            <input
              id="local-release"
              type="checkbox"
              className="size-5"
              checked={enabled}
              disabled={!policy || saving}
              onChange={(event) => {
                setEnabled(event.target.checked);
                if (event.target.checked) setDays(180);
              }}
            />
            {t("folder_settings.retention_auto")}
          </label>
          <FormField label={t("folder_settings.retention_period")} htmlFor="local-release-period" help={t("folder_settings.retention_period_help")}>
            <FormSelect
              id="local-release-period"
              disabled={!enabled || saving}
              value={days}
              onChange={(event) => {
                const selected = periods.find((period) => String(period) === event.target.value);
                if (selected) setDays(selected);
              }}
            >
              {periods.map((period) => <option key={period} value={period}>{t(`folder_settings.retention_period_${period}`)}</option>)}
            </FormSelect>
          </FormField>
          <Button type="submit" className="min-h-11 w-full sm:w-fit sm:justify-self-start" disabled={saving || !policy}>
            {t("folder_settings.retention_save")}
          </Button>
        </form>
      </div>
    </section>
  );
}

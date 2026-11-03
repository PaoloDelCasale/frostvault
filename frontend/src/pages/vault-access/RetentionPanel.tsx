import { useEffect, useState } from "react";

import {
  fetchOperationPolicy,
  updateOperationPolicy,
  type OperationPolicy,
} from "@/api";
import { FormField, FormSelect } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

type RetentionPanelProps = {
  onNotice: (message: string, error?: boolean) => void;
};
type ReleasePeriod = NonNullable<OperationPolicy["local_release_after_days"]>;
const periods: ReleasePeriod[] = [90, 180, 365, 730];

export function RetentionPanel({ onNotice }: RetentionPanelProps) {
  const { t, ready } = useI18n();
  const [loadState, setLoadState] = useState("");
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
    setLoadState(t("access.retention_loading"));
    void (async () => {
      try {
        acceptPolicy(await fetchOperationPolicy());
        setLoadState(t("access.retention_loaded"));
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setLoadState(message);
        onNotice(message, true);
      }
    })();
  }, [onNotice, ready, t]);

  async function onSave(event: React.FormEvent) {
    event.preventDefault();
    if (!policy) return;
    setSaving(true);
    try {
      acceptPolicy(await updateOperationPolicy({
        local_release_after_days: enabled ? days : null,
      }));
      onNotice(t(enabled ? "access.retention_enabled" : "access.retention_disabled"));
    } catch (error) {
      onNotice(error instanceof Error ? error.message : String(error), true);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section data-panel="retention">
      <Panel className="p-4 sm:p-5">
        <h2 className="text-lg font-bold">{t("access.retention_title")}</h2>
        <p className="mt-1 text-sm text-muted">{t("access.retention_help")}</p>
        <p className="mt-2 text-sm text-muted">{t("access.retention_example")}</p>
        <p className="mt-2 text-sm text-muted">{t("access.retention_never")}</p>
        <p className="mt-2 text-sm text-muted" role="status">{loadState}</p>
        <form className="mt-4 grid gap-3" onSubmit={(event) => void onSave(event)}>
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
            {t("access.retention_auto")}
          </label>
          <FormField label={t("access.retention_period")} htmlFor="local-release-period" help={t("access.retention_period_help")}>
            <FormSelect
              id="local-release-period"
              disabled={!enabled || saving}
              value={days}
              onChange={(event) => {
                const selected = periods.find((period) => String(period) === event.target.value);
                if (selected) setDays(selected);
              }}
            >
              {periods.map((period) => <option key={period} value={period}>{t(`access.retention_period_${period}`)}</option>)}
            </FormSelect>
          </FormField>
          <Button type="submit" className="min-h-11 w-full sm:w-auto" disabled={saving || !policy}>
            {t("access.retention_save")}
          </Button>
        </form>
      </Panel>
    </section>
  );
}

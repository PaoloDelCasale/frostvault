import { useEffect, useState } from "react";

import {
  fetchOperationPolicy,
  updateOperationPolicy,
  type OperationPolicy,
} from "@/api";
import { FormField, FormInput } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

type RetentionPanelProps = {
  onNotice: (message: string, error?: boolean) => void;
};

export function RetentionPanel({ onNotice }: RetentionPanelProps) {
  const { t, ready } = useI18n();
  const [loadState, setLoadState] = useState("");
  const [policy, setPolicy] = useState<OperationPolicy | null>(null);
  const [autoCleanup, setAutoCleanup] = useState(false);
  const [days, setDays] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!ready) return;
    setLoadState(t("access.retention_loading"));
    void (async () => {
      try {
        const data = await fetchOperationPolicy();
        setPolicy(data);
        setAutoCleanup(Boolean(data.auto_local_cleanup));
        setDays(
          data.local_retention_days === null ||
            data.local_retention_days === undefined
            ? ""
            : String(data.local_retention_days),
        );
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
    const retentionDays = days === "" ? null : Number(days);
    if (
      autoCleanup &&
      (!Number.isInteger(retentionDays) || (retentionDays ?? 0) < 1)
    ) {
      onNotice(t("access.retention_days_required"), true);
      return;
    }
    setSaving(true);
    try {
      const updated = await updateOperationPolicy({
        ...policy,
        auto_local_cleanup: autoCleanup,
        local_retention_days: retentionDays,
      });
      setPolicy(updated);
      setAutoCleanup(Boolean(updated.auto_local_cleanup));
      setDays(
        updated.local_retention_days === null ||
          updated.local_retention_days === undefined
          ? ""
          : String(updated.local_retention_days),
      );
      onNotice(
        autoCleanup
          ? t("access.retention_enabled")
          : t("access.retention_disabled"),
      );
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
        <p className="mt-2 text-sm text-muted" role="status">
          {loadState}
        </p>
        <form className="mt-4 grid gap-3" onSubmit={(event) => void onSave(event)}>
          <label className="flex min-h-11 items-center gap-3 text-sm font-bold text-ink">
            <input
              id="auto-local-cleanup"
              type="checkbox"
              className="size-5"
              checked={autoCleanup}
              onChange={(event) => setAutoCleanup(event.target.checked)}
            />
            {t("access.retention_auto")}
          </label>
          <FormField label={t("access.retention_days")} htmlFor="local-retention-days" help={t("access.retention_days_help")}>
            <FormInput
              id="local-retention-days"
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              disabled={!autoCleanup}
              value={days}
              onChange={(event) => setDays(event.target.value)}
            />
          </FormField>
          <Button type="submit" className="min-h-11 w-full sm:w-auto" disabled={saving || !policy}>
            {t("access.retention_save")}
          </Button>
        </form>
      </Panel>
    </section>
  );
}

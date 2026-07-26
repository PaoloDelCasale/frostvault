import { useEffect, useState } from "react";

import { fetchCloudDeletion, updateCloudDeletion } from "@/api";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

type CloudDeletionPanelProps = {
  onNotice: (message: string, error?: boolean) => void;
};

export function CloudDeletionPanel({ onNotice }: CloudDeletionPanelProps) {
  const { t, ready } = useI18n();
  const [loadState, setLoadState] = useState("");
  const [help, setHelp] = useState("");
  const [risk, setRisk] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loaded, setLoaded] = useState(false);

  async function load() {
    try {
      const data = await fetchCloudDeletion();
      setEnabled(Boolean(data.enabled));
      setHelp(data.delete_marker_explanation || t("access.cloud_deletion_help"));
      setRisk(data.accepted_single_identity_risk || "");
      setLoadState(
        data.enabled
          ? t("access.cloud_deletion_enabled_state")
          : t("access.cloud_deletion_disabled_state"),
      );
      setLoaded(true);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setLoadState(message);
      onNotice(message, true);
    }
  }

  useEffect(() => {
    if (!ready) return;
    setLoadState(t("access.cloud_deletion_loading"));
    setHelp(t("access.cloud_deletion_help"));
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load when i18n ready
  }, [ready]);

  async function onSave(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      const data = await updateCloudDeletion(enabled);
      onNotice(
        data.enabled
          ? t("api.cloud_deletion_enabled")
          : t("api.cloud_deletion_disabled"),
      );
      await load();
    } catch (error) {
      onNotice(error instanceof Error ? error.message : String(error), true);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section data-panel="cloud-deletion">
      <Panel className="p-4 sm:p-5">
        <h2 className="text-lg font-bold">{t("access.cloud_deletion_title")}</h2>
        <p className="mt-1 text-sm text-muted">{help}</p>
        <p className="mt-2 text-sm text-muted" role="status">
          {loadState}
        </p>
        {risk ? <p className="mt-2 text-sm text-muted">{risk}</p> : null}
        <form className="mt-4 grid gap-3" onSubmit={(event) => void onSave(event)}>
          <label className="flex min-h-11 items-center gap-3 text-sm font-bold text-ink">
            <input
              id="cloud-deletion-enabled"
              type="checkbox"
              className="size-5"
              checked={enabled}
              onChange={(event) => setEnabled(event.target.checked)}
            />
            {t("access.cloud_deletion_toggle")}
          </label>
          <Button
            type="submit"
            className="min-h-11 w-full sm:w-auto"
            disabled={saving || !loaded}
          >
            {t("access.cloud_deletion_save")}
          </Button>
        </form>
      </Panel>
    </section>
  );
}

import { useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";

import { fetchLifecycle, updateLifecycleDefault, type LifecycleProfile, type LifecycleResponse } from "@/api";
import { FormField, FormInput } from "@/components/FormField";
import { MenuSelect } from "@/components/MenuSelect";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

import { LIFECYCLE_CLASSES, validStoragePlan, type DraftTransition } from "./storagePlan";
import { VaultMovePanel } from "./VaultMovePanel";

const PRESETS = ["standard_only", "economical", "archive_tiered", "deep_archive_early"];

export function LifecyclePanel({ vaultId, onNotice }: { vaultId: number; onNotice: (message: string, error?: boolean) => void }) {
  const { t, ready } = useI18n();
  const [data, setData] = useState<LifecycleResponse | null>(null);
  const [selection, setSelection] = useState("archive_tiered");
  const [rules, setRules] = useState<DraftTransition[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  function apply(next: LifecycleResponse) {
    setData(next);
    const signature = JSON.stringify(next.profile.transitions);
    setSelection(Object.entries(next.guided_profiles).find(([, profile]) => JSON.stringify(profile.transitions) === signature)?.[0] ?? "custom");
    setRules(next.profile.transitions.map((rule) => ({ days: String(rule.days), storage_class: rule.storage_class })));
  }

  useEffect(() => {
    if (!ready) return;
    void fetchLifecycle(vaultId).then(apply).catch((cause: unknown) => setError(String(cause)));
  }, [ready, vaultId]);

  async function save() {
    if (selection === "custom" && !validStoragePlan(rules)) {
      setError(t("folder_settings.plan_invalid"));
      return;
    }
    setBusy(true);
    setError("");
    try {
      const profile: LifecycleProfile = { transitions: rules.map((rule) => ({ days: Number(rule.days), storage_class: rule.storage_class })) };
      const next = await updateLifecycleDefault(selection === "custom" ? { profile } : selection, vaultId);
      apply(next);
      onNotice(t(next.sync_pending ? "folder_settings.plan_sync_pending" : "folder_settings.plan_saved"));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section data-panel="lifecycle">
      <Panel className="p-4 sm:p-5">
        <h2 className="text-lg font-bold text-ink">{t("folder_settings.plan_title")}</h2>
        <p className="mt-1 text-sm text-muted">{t("folder_settings.plan_help")}</p>
        {data ? <div className="mt-4 grid gap-4">
          <FormField label={t("folder_settings.plan_label")} htmlFor="vault-storage-plan" labelAsText>
            <MenuSelect id="vault-storage-plan" label={t("folder_settings.plan_label")} value={selection} disabled={busy}
              onValueChange={(value) => {
                setSelection(value);
                setError("");
                const preset = data.guided_profiles[value];
                if (preset) {
                  setRules(preset.transitions.map((rule) => ({ days: String(rule.days), storage_class: rule.storage_class })));
                }
              }}
              options={[...PRESETS, "custom"].map((name) => ({ value: name, label: t(`folder_settings.plan_${name}`), description: t(`folder_settings.plan_${name}_help`) }))} />
          </FormField>
          <p className="text-sm text-muted">{t(`folder_settings.plan_${selection}_help`)}</p>
          {selection === "custom" && <div className="grid gap-3" data-testid="storage-plan-editor">
            {rules.map((rule, index) => <div key={index} className="flex flex-wrap items-end gap-2">
              <FormField label={t("folder_settings.plan_days")} htmlFor={`plan-days-${index}`}>
                <FormInput id={`plan-days-${index}`} type="number" min="0" step="1" value={rule.days} disabled={busy}
                  onChange={(event) => setRules(rules.map((item, i) => i === index ? { ...item, days: event.target.value } : item))} />
              </FormField>
              <FormField label={t("folder_settings.plan_class")} htmlFor={`plan-class-${index}`} labelAsText>
                <MenuSelect id={`plan-class-${index}`} label={t("folder_settings.plan_class")} value={rule.storage_class} disabled={busy}
                  onValueChange={(value) => setRules(rules.map((item, i) => i === index ? { ...item, storage_class: value } : item))}
                  options={LIFECYCLE_CLASSES.map((value) => ({ value, label: t(`storage.${value}`) }))} />
              </FormField>
              <Button type="button" variant="secondary" disabled={busy} aria-label={t("folder_settings.plan_remove")} onClick={() => setRules(rules.filter((_, i) => i !== index))}><Trash2 size={16} /></Button>
            </div>)}
            <Button type="button" variant="secondary" className="w-fit" disabled={busy || rules.length >= LIFECYCLE_CLASSES.length} onClick={() => setRules([...rules, { days: "", storage_class: "DEEP_ARCHIVE" }])}><Plus size={16} />{t("folder_settings.plan_add")}</Button>
          </div>}
          <p className="text-sm text-muted">{t("folder_settings.plan_caveats")}</p>
          <Button type="button" variant="primary" disabled={busy} className="w-fit" onClick={() => void save()}>{t("folder_settings.plan_save")}</Button>
        </div> : <p role="status">{t("folder_settings.plan_loading")}</p>}
        {error && <p role="alert" className="mt-3 text-sm text-red-700">{error}</p>}
        {data && <VaultMovePanel key={`${vaultId}:${JSON.stringify(data.profile)}`} vaultId={vaultId} onNotice={onNotice} />}
      </Panel>
    </section>
  );
}

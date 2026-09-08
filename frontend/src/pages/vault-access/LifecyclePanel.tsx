import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";

import {
  deleteLifecycleFolderOverride,
  fetchLifecycle,
  fetchStorageClasses,
  startStorageClass,
  updateLifecycleDefault,
  upsertLifecycleFolderOverride,
  type LifecycleGuidedProfile,
  type LifecycleProfile,
  type LifecycleResponse,
} from "@/api";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { FormField, FormInput } from "@/components/FormField";
import { MenuSelect, type MenuSelectOption } from "@/components/MenuSelect";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import {
  COLD_STORAGE_CLASSES,
  STORAGE_CLASS_OPTIONS,
  type ManualStorageClass,
} from "@/pages/archive/actions";
import { StorageClassSelect } from "@/pages/archive/StorageClassSelect";
import type { StorageClassOption } from "@/pages/archive/storageClassOptions";

const CUSTOM_PROFILE = "__custom__";
const LIFECYCLE_CLASSES = [
  "STANDARD_IA",
  "ONEZONE_IA",
  "GLACIER_IR",
  "GLACIER",
  "DEEP_ARCHIVE",
] as const;
const CLASS_DEPTH: Record<string, number> = {
  STANDARD: 0,
  STANDARD_IA: 1,
  ONEZONE_IA: 1,
  GLACIER_IR: 2,
  GLACIER: 3,
  DEEP_ARCHIVE: 4,
};
const MIN_DAYS: Record<string, number> = {
  STANDARD_IA: 30,
  ONEZONE_IA: 30,
  GLACIER_IR: 0,
  GLACIER: 90,
  DEEP_ARCHIVE: 180,
};

type LifecyclePanelProps = {
  onNotice: (message: string, error?: boolean) => void;
};

type DraftRule = { days: string; storage_class: string };
type RuleErrors = { days?: string; storage_class?: string };
type EditorTarget = "default" | "folder";

function normalizeProfile(profile?: LifecycleGuidedProfile): LifecycleProfile {
  return {
    transitions: (profile?.transitions ?? []).map((rule) => ({ ...rule })),
    expiration_days: profile?.expiration_days ?? null,
    noncurrent_expiration_days: profile?.noncurrent_expiration_days ?? null,
    noncurrent_transitions: (profile?.noncurrent_transitions ?? []).map((rule) => ({
      ...rule,
    })),
  };
}

type LifecycleTranslate = (
  key: string,
  params?: Record<string, unknown>,
) => string;

function readableProfileName(name: string, t: LifecycleTranslate): string {
  const knownNames: Record<string, string> = {
    standard_only: t("access.lifecycle_profile_standard"),
    ia_after_30: t("access.lifecycle_profile_ia"),
    archive_tiered: t("access.lifecycle_profile_archive"),
  };
  if (knownNames[name]) return knownNames[name];
  return name
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function readableStorageClass(storageClass: string, t: LifecycleTranslate): string {
  const knownClasses: Record<string, string> = {
    STANDARD: t("access.lifecycle_class_standard"),
    STANDARD_IA: t("access.lifecycle_class_standard_ia"),
    ONEZONE_IA: t("access.lifecycle_class_onezone_ia"),
    GLACIER_IR: t("access.lifecycle_class_glacier_ir"),
    GLACIER: t("access.lifecycle_class_glacier"),
    DEEP_ARCHIVE: t("access.lifecycle_class_deep_archive"),
  };
  return knownClasses[storageClass] ?? storageClass;
}

function storageClassDescription(storageClass: string, t: LifecycleTranslate): string {
  const descriptions: Record<string, string> = {
    STANDARD: t("access.lifecycle_class_standard_help"),
    STANDARD_IA: t("access.lifecycle_class_standard_ia_help"),
    ONEZONE_IA: t("access.lifecycle_class_onezone_ia_help"),
    GLACIER_IR: t("access.lifecycle_class_glacier_ir_help"),
    GLACIER: t("access.lifecycle_class_glacier_help"),
    DEEP_ARCHIVE: t("access.lifecycle_class_deep_archive_help"),
  };
  return descriptions[storageClass] ?? "";
}

function profileTimeline(
  profile: LifecycleGuidedProfile | undefined,
  t: LifecycleTranslate,
): string {
  if (!profile?.transitions?.length) {
    return t("access.lifecycle_profile_no_transitions");
  }
  return profile.transitions
    .map((step) =>
      t("access.lifecycle_profile_step", {
        storageClass: readableStorageClass(step.storage_class, t),
        days: step.days,
      }),
    )
    .join(" → ");
}

function profileLabel(
  name: string,
  profile: LifecycleGuidedProfile | undefined,
  t: LifecycleTranslate,
): string {
  return `${readableProfileName(name, t)} · ${profileTimeline(profile, t)}`;
}

function profileSignature(profile?: LifecycleGuidedProfile): string {
  return JSON.stringify(normalizeProfile(profile));
}

function selectedDefaultProfile(data: LifecycleResponse): string {
  const defaultPolicy = (data.policies || []).find(
    (item) => item.id === data.default_policy_id,
  );
  if (!defaultPolicy?.profile) return "standard_only";
  const signature = profileSignature(defaultPolicy.profile);
  for (const [name, profile] of Object.entries(data.guided_profiles || {})) {
    if (profileSignature(profile) === signature) return name;
  }
  return CUSTOM_PROFILE;
}

function draftRules(profile: LifecycleProfile, noncurrent = false): DraftRule[] {
  const rules = noncurrent ? profile.noncurrent_transitions : profile.transitions;
  return rules.map((rule) => ({
    days: String(rule.days),
    storage_class: rule.storage_class,
  }));
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
  const [editorTarget, setEditorTarget] = useState<EditorTarget | null>(null);
  const [editorBase, setEditorBase] = useState<LifecycleProfile | null>(null);
  const [currentRules, setCurrentRules] = useState<DraftRule[]>([]);
  const [noncurrentRules, setNoncurrentRules] = useState<DraftRule[]>([]);
  const [noncurrentEnabled, setNoncurrentEnabled] = useState(false);
  const [showErrors, setShowErrors] = useState(false);
  const [vaultClassOpen, setVaultClassOpen] = useState(false);
  const [vaultTarget, setVaultTarget] = useState<ManualStorageClass>("DEEP_ARCHIVE");
  const [classOptions, setClassOptions] = useState<StorageClassOption[]>([]);

  useEffect(() => {
    void fetchStorageClasses()
      .then((payload) => setClassOptions(payload.items as StorageClassOption[]))
      .catch(() => setClassOptions([]));
  }, []);

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
    void fetchLifecycle()
      .then(applyLifecycle)
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        setLoadState(message);
        onNotice(message, true);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load when i18n ready
  }, [ready]);

  const profiles = Object.entries(data?.guided_profiles ?? {});
  const guidedProfileOptions: MenuSelectOption[] = profiles.map(([name, profile]) => ({
    value: name,
    label: readableProfileName(name, t),
    description: profileTimeline(profile, t),
  }));
  const defaultProfileOptions: MenuSelectOption[] = [
    ...guidedProfileOptions,
    ...(defaultProfile === CUSTOM_PROFILE
      ? [{
          value: CUSTOM_PROFILE,
          label: t("access.lifecycle_custom_profile"),
          description: t("access.lifecycle_custom_profile_help"),
        }]
      : []),
  ];
  const hasValidDefaultProfile = defaultProfileOptions.some(
    (option) => option.value === defaultProfile,
  );
  const hasValidFolderProfile = guidedProfileOptions.some(
    (option) => option.value === folderProfile,
  );
  const lifecycleClassOptions: MenuSelectOption[] = [
    { value: "", label: t("access.lifecycle_choose_class") },
    ...LIFECYCLE_CLASSES.map((storageClass) => ({
      value: storageClass,
      label: readableStorageClass(storageClass, t),
      description: storageClassDescription(storageClass, t),
    })),
  ];

  function profileForDefault(): LifecycleProfile {
    if (defaultProfile !== CUSTOM_PROFILE) {
      return normalizeProfile(data?.guided_profiles[defaultProfile]);
    }
    return normalizeProfile(
      data?.policies.find((policy) => policy.id === data.default_policy_id)?.profile,
    );
  }

  function openEditor(target: EditorTarget, profile: LifecycleProfile) {
    setEditorTarget(target);
    setEditorBase(profile);
    setCurrentRules(draftRules(profile));
    setNoncurrentRules(draftRules(profile, true));
    setNoncurrentEnabled(profile.noncurrent_transitions.length > 0);
    setShowErrors(false);
  }

  function customizeGuided(target: EditorTarget) {
    const name = target === "default" ? defaultProfile : folderProfile;
    openEditor(
      target,
      name === CUSTOM_PROFILE
        ? profileForDefault()
        : normalizeProfile(data?.guided_profiles[name]),
    );
  }

  function validateRules(rules: DraftRule[], requireOne: boolean): RuleErrors[] {
    if (requireOne && rules.length === 0) return [{ days: t("access.lifecycle_error_incomplete") }];
    let previousDays = 0;
    let previousDepth = 0;
    return rules.map((rule) => {
      const errors: RuleErrors = {};
      const days = Number(rule.days);
      if (!rule.days || !Number.isInteger(days) || days <= 0) {
        errors.days = t("access.lifecycle_error_incomplete");
      } else if (days <= previousDays) {
        errors.days = t("access.lifecycle_error_days_absolute");
      }
      const depth = CLASS_DEPTH[rule.storage_class];
      if (!rule.storage_class || depth === undefined) {
        errors.storage_class = t("access.lifecycle_error_incomplete");
      } else if (depth <= previousDepth) {
        errors.storage_class = t("access.lifecycle_error_depth", {
          storageClass: rule.storage_class,
        });
      }
      const minimum = MIN_DAYS[rule.storage_class];
      if (
        Number.isInteger(days) &&
        minimum !== undefined &&
        days < minimum
      ) {
        errors.days = t("access.lifecycle_error_minimum", {
          storageClass: rule.storage_class,
          days: minimum,
        });
      }
      if (Number.isInteger(days) && days > 0) previousDays = days;
      if (depth !== undefined) previousDepth = depth;
      return errors;
    });
  }

  const currentErrors = validateRules(currentRules, true);
  const noncurrentErrors = noncurrentEnabled
    ? validateRules(noncurrentRules, true)
    : [];
  const editorInvalid = [...currentErrors, ...noncurrentErrors].some(
    (error) => error.days || error.storage_class,
  );
  const editorCold = [...currentRules, ...(noncurrentEnabled ? noncurrentRules : [])].some(
    (rule) => COLD_STORAGE_CLASSES.has(rule.storage_class as ManualStorageClass),
  );

  function updateRule(
    list: DraftRule[],
    setList: (rules: DraftRule[]) => void,
    index: number,
    patch: Partial<DraftRule>,
  ) {
    setList(list.map((rule, ruleIndex) => (ruleIndex === index ? { ...rule, ...patch } : rule)));
  }

  function moveRule(
    list: DraftRule[],
    setList: (rules: DraftRule[]) => void,
    index: number,
    delta: number,
  ) {
    const nextIndex = index + delta;
    if (nextIndex < 0 || nextIndex >= list.length) return;
    const next = [...list];
    [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
    setList(next);
  }

  function renderRules(
    rules: DraftRule[],
    setRules: (rules: DraftRule[]) => void,
    errors: RuleErrors[],
    prefix: string,
  ) {
    return (
      <div className="grid min-w-0 gap-3">
        <div className="flex items-center gap-2 rounded-lg bg-green-soft/45 px-3 py-2 text-sm">
          <span className="size-2 shrink-0 rounded-full bg-green" aria-hidden="true" />
          <span className="font-bold">{t("access.lifecycle_starts_standard")}</span>
        </div>
        {rules.map((rule, index) => (
          <div key={`${prefix}-${index}`} className="relative grid min-w-0 gap-3 rounded-[14px] border border-line bg-canvas p-3 sm:p-4">
            <div className="flex min-w-0 items-center justify-between gap-2">
              <strong className="text-sm">{t("access.lifecycle_transition_number", { number: index + 1 })}</strong>
              <div className="flex shrink-0 gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="size-9 min-h-9"
                  disabled={index === 0}
                  aria-label={t("access.lifecycle_move_up")}
                  title={t("access.lifecycle_move_up")}
                  onClick={() => moveRule(rules, setRules, index, -1)}
                >
                  <ArrowUp aria-hidden="true" />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="size-9 min-h-9"
                  disabled={index === rules.length - 1}
                  aria-label={t("access.lifecycle_move_down")}
                  title={t("access.lifecycle_move_down")}
                  onClick={() => moveRule(rules, setRules, index, 1)}
                >
                  <ArrowDown aria-hidden="true" />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="size-9 min-h-9 text-danger"
                  aria-label={t("access.lifecycle_remove_rule")}
                  title={t("access.lifecycle_remove_rule")}
                  onClick={() => setRules(rules.filter((_, ruleIndex) => ruleIndex !== index))}
                >
                  <Trash2 aria-hidden="true" />
                </Button>
              </div>
            </div>
            <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-[minmax(9rem,0.65fr)_minmax(0,1.35fr)]">
              <FormField
                label={t("access.lifecycle_rule_days_short")}
                htmlFor={`${prefix}-days-${index}`}
              >
                <div className="relative">
                  <FormInput
                    id={`${prefix}-days-${index}`}
                    className="pr-16"
                    type="number"
                    inputMode="numeric"
                    min={1}
                    value={rule.days}
                    aria-invalid={showErrors && Boolean(errors[index]?.days)}
                    onChange={(event) =>
                      updateRule(rules, setRules, index, { days: event.target.value })
                    }
                  />
                  <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-sm font-medium text-muted">
                    {t("access.lifecycle_days_suffix")}
                  </span>
                </div>
                {showErrors && errors[index]?.days ? (
                  <span className="font-medium text-danger" role="alert">{errors[index].days}</span>
                ) : null}
              </FormField>
              <FormField
                label={t("access.lifecycle_rule_class")}
                htmlFor={`${prefix}-class-${index}`}
                labelAsText
              >
                <MenuSelect
                  id={`${prefix}-class-${index}`}
                  value={rule.storage_class}
                  options={lifecycleClassOptions}
                  label={t("access.lifecycle_rule_class")}
                  invalid={showErrors && Boolean(errors[index]?.storage_class)}
                  onValueChange={(storageClass) =>
                    updateRule(rules, setRules, index, { storage_class: storageClass })
                  }
                />
                {showErrors && errors[index]?.storage_class ? (
                  <span className="font-medium text-danger" role="alert">{errors[index].storage_class}</span>
                ) : null}
              </FormField>
            </div>
            {rule.days && rule.storage_class ? (
              <p className="text-xs text-muted">
                {t("access.lifecycle_transition_summary", {
                  days: rule.days,
                  storageClass: rule.storage_class,
                })}
              </p>
            ) : null}
          </div>
        ))}
        <Button type="button" variant="secondary" className="min-h-11 w-full sm:w-auto" onClick={() => setRules([...rules, { days: "", storage_class: "" }])}>
          <Plus aria-hidden="true" />
          {t("access.lifecycle_add_transition")}
        </Button>
      </div>
    );
  }

  async function onSaveDefault(event: React.FormEvent) {
    event.preventDefault();
    if (!hasValidDefaultProfile) {
      onNotice(t("access.lifecycle_choose_profile_error"), true);
      return;
    }
    if (defaultProfile === CUSTOM_PROFILE) {
      openEditor("default", profileForDefault());
      return;
    }
    setBusy(true);
    try {
      const next = await updateLifecycleDefault(defaultProfile);
      applyLifecycle(next);
      onNotice((next.warnings || []).join(" ") || t("api.lifecycle_profile_updated"));
    } catch (error) {
      onNotice(error instanceof Error ? error.message : String(error), true);
    } finally {
      setBusy(false);
    }
  }

  async function onSaveOverride(event: React.FormEvent) {
    event.preventDefault();
    if (!folderPath.trim() || !hasValidFolderProfile) {
      onNotice(t("access.lifecycle_override_incomplete"), true);
      return;
    }
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

  async function onSaveCustom() {
    setShowErrors(true);
    if (editorInvalid || !editorBase || !editorTarget) return;
    const profile: LifecycleProfile = {
      ...editorBase,
      transitions: currentRules.map((rule) => ({
        days: Number(rule.days),
        storage_class: rule.storage_class,
      })),
      noncurrent_transitions: noncurrentEnabled
        ? noncurrentRules.map((rule) => ({
            days: Number(rule.days),
            storage_class: rule.storage_class,
          }))
        : [],
    };
    setBusy(true);
    try {
      const next =
        editorTarget === "default"
          ? await updateLifecycleDefault({ profile })
          : await upsertLifecycleFolderOverride(folderPath, { profile });
      applyLifecycle(next);
      if (editorTarget === "folder") setFolderPath("");
      setEditorTarget(null);
      onNotice(
        (next.warnings || []).join(" ") ||
          t(editorTarget === "default" ? "api.lifecycle_profile_updated" : "api.lifecycle_override_updated"),
      );
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
      <Panel className="min-w-0 overflow-hidden p-4 sm:p-5">
        <h2 className="text-lg font-bold">{t("access.lifecycle_title")}</h2>
        <p className="mt-1 text-sm text-muted">{t("access.lifecycle_help")}</p>
        <p className="mt-2 text-sm text-muted" role="status">{loadState}</p>

        <form className="mt-4 grid min-w-0 gap-3" onSubmit={(event) => void onSaveDefault(event)}>
          <FormField label={t("access.lifecycle_default")} htmlFor="lifecycle-default-profile" labelAsText>
            <MenuSelect
              id="lifecycle-default-profile"
              value={defaultProfile}
              options={defaultProfileOptions}
              label={t("access.lifecycle_default")}
              onValueChange={setDefaultProfile}
            />
          </FormField>
          <div className="grid grid-cols-1 gap-2 sm:flex">
            <Button type="submit" className="min-h-11 w-full sm:w-auto" disabled={busy || !data || !hasValidDefaultProfile}>
              {defaultProfile === CUSTOM_PROFILE ? t("access.lifecycle_edit_custom") : t("access.lifecycle_save_default")}
            </Button>
            <Button type="button" variant="secondary" className="min-h-11 w-full sm:w-auto" disabled={busy || !data} onClick={() => customizeGuided("default")}>
              {t("access.lifecycle_customize")}
            </Button>
          </div>
        </form>

        <form className="mt-6 grid min-w-0 gap-3 border-t border-line pt-4" onSubmit={(event) => void onSaveOverride(event)}>
          <h3 className="text-sm font-bold">{t("access.lifecycle_override")}</h3>
          <FormField label={t("access.lifecycle_folder_path")} htmlFor="lifecycle-folder-path">
            <FormInput id="lifecycle-folder-path" maxLength={500} required value={folderPath} onChange={(event) => setFolderPath(event.target.value)} placeholder="photos/2024" />
          </FormField>
          <FormField label={t("access.lifecycle_folder_profile")} htmlFor="lifecycle-folder-profile" labelAsText>
            <MenuSelect
              id="lifecycle-folder-profile"
              value={folderProfile}
              options={guidedProfileOptions}
              label={t("access.lifecycle_folder_profile")}
              onValueChange={setFolderProfile}
            />
          </FormField>
          <div className="grid grid-cols-1 gap-2 sm:flex">
            <Button type="submit" className="min-h-11 w-full sm:w-auto" disabled={busy || !data || !folderPath.trim() || !hasValidFolderProfile}>{t("access.lifecycle_save_override")}</Button>
            <Button type="button" variant="secondary" className="min-h-11 w-full sm:w-auto" disabled={busy || !data || !folderPath.trim()} onClick={() => customizeGuided("folder")}>
              {t("access.lifecycle_customize")}
            </Button>
          </div>
        </form>

        {editorTarget ? (
          <div data-lifecycle-editor className="mt-6 grid min-w-0 gap-4 border-t border-line pt-4">
            <div>
              <h3 className="font-bold">{t("access.lifecycle_custom_title")}</h3>
              <p className="text-sm text-muted">{t("access.lifecycle_absolute_help")}</p>
            </div>
            <div className="grid gap-2">
              <h4 className="text-sm font-bold">{t("access.lifecycle_current_rules")}</h4>
              {renderRules(currentRules, setCurrentRules, currentErrors, "current")}
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={noncurrentEnabled}
              className="flex min-h-11 w-full items-center justify-between gap-3 rounded-[14px] border border-line bg-canvas px-3 py-2 text-left text-sm font-bold outline-none transition-colors hover:bg-green-soft/45 focus-visible:ring-3 focus-visible:ring-ring/50 motion-reduce:transition-none"
              onClick={() => {
                const enabled = !noncurrentEnabled;
                setNoncurrentEnabled(enabled);
                if (enabled && noncurrentRules.length === 0) {
                  setNoncurrentRules([{ days: "", storage_class: "" }]);
                }
              }}
            >
              <span>
                <span className="block">{t("access.lifecycle_enable_noncurrent")}</span>
                <span className="mt-0.5 block text-xs font-medium text-muted">{t("access.lifecycle_noncurrent_help")}</span>
              </span>
              <span
                className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${noncurrentEnabled ? "bg-green" : "bg-line"}`}
                aria-hidden="true"
              >
                <span className={`absolute top-1 size-4 rounded-full bg-white shadow-sm transition-transform ${noncurrentEnabled ? "translate-x-6" : "translate-x-1"}`} />
              </span>
            </button>
            {noncurrentEnabled ? (
              <div className="grid gap-2">
                <h4 className="text-sm font-bold">{t("access.lifecycle_noncurrent_rules")}</h4>
                {renderRules(noncurrentRules, setNoncurrentRules, noncurrentErrors, "noncurrent")}
              </div>
            ) : null}
            {editorCold ? <p className="text-sm text-amber-800" role="status">{t("access.lifecycle_cold_warning")}</p> : null}
            <div className="grid grid-cols-1 gap-2 sm:flex">
              <Button type="button" className="min-h-11 w-full sm:w-auto" disabled={busy} onClick={() => void onSaveCustom()}>{t("access.lifecycle_save_custom")}</Button>
              <Button type="button" variant="secondary" className="min-h-11 w-full sm:w-auto" disabled={busy} onClick={() => setEditorTarget(null)}>{t("ui.cancel")}</Button>
            </div>
          </div>
        ) : null}

        <div className="mt-6 grid gap-3 border-t border-line pt-4">
          <h3 className="text-sm font-bold">{t("ui.vault_storage_class_action")}</h3>
          <p className="text-sm text-muted">{t("ui.vault_storage_class_hint")}</p>
          <FormField label={t("ui.storage_class_picker_label")} htmlFor="vault-storage-class-target" labelAsText>
            <StorageClassSelect
              id="vault-storage-class-target"
              value={vaultTarget}
              options={classOptions.length ? classOptions : STORAGE_CLASS_OPTIONS.map((id): StorageClassOption => ({
                id,
                currency: "EUR",
                storage_rate_eur_per_gib_month: 0,
                retrieval: COLD_STORAGE_CLASSES.has(id) && id !== "GLACIER_IR" ? "restore" : "instant",
                min_duration_days: 0,
                requires_restore: id === "GLACIER" || id === "DEEP_ARCHIVE",
                availability_zones: id === "ONEZONE_IA" ? "single" : "multi",
              }))}
              onValueChange={(next) => setVaultTarget(next as ManualStorageClass)}
              t={t}
            />
          </FormField>
          {COLD_STORAGE_CLASSES.has(vaultTarget) ? <p className="text-sm text-amber-800">{t("ui.storage_class_confirm_warning")}</p> : null}
          <Button type="button" className="min-h-11 w-full sm:w-auto" disabled={busy} onClick={() => setVaultClassOpen(true)}>{t("ui.vault_storage_class_action")}</Button>
        </div>

        <div className="mt-4 grid min-w-0 gap-2">
          {(data?.folder_overrides ?? []).length === 0 ? (
            <p className="text-sm text-muted">{t("access.lifecycle_no_overrides")}</p>
          ) : (
            (data?.folder_overrides ?? []).map((item) => {
              const policy = (data?.policies ?? []).find((row) => row.id === item.policy_id);
              const label = policy?.profile
                ? profileLabel(policy.name ?? t("access.lifecycle_custom_profile"), policy.profile, t)
                : policy?.name ?? String(item.policy_id);
              return (
                <div key={item.folder_path} className="grid min-w-0 gap-2 rounded-[14px] border border-line bg-canvas p-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                  <div className="min-w-0">
                    <strong className="block break-words">{item.folder_path}</strong>
                    <small className="block break-words text-muted">{label}</small>
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    {policy?.profile ? (
                      <Button type="button" variant="secondary" className="min-h-11" disabled={busy} onClick={() => {
                        setFolderPath(item.folder_path);
                        openEditor("folder", normalizeProfile(policy.profile));
                      }}>{t("access.lifecycle_edit_custom")}</Button>
                    ) : null}
                    <Button type="button" variant="secondary" className="min-h-11" disabled={busy} onClick={() => void onRemoveOverride(item.folder_path)}>{t("access.lifecycle_remove_override")}</Button>
                  </div>
                </div>
              );
            })
          )}
        </div>
        {warnings ? <p className="mt-3 text-sm text-muted" role="status">{warnings}</p> : null}
      </Panel>
      <ConfirmDialog
        open={vaultClassOpen}
        onOpenChange={setVaultClassOpen}
        title={t("ui.storage_class_confirm_title")}
        description={`${t("ui.storage_class_confirm_body", { count: "all", bytes: "—", target: vaultTarget })}\n${t("ui.storage_class_policy_note")}`}
        confirmLabel={t("ui.vault_storage_class_action")}
        cancelLabel={t("ui.cancel")}
        tone="default"
        onConfirm={() => {
          setVaultClassOpen(false);
          setBusy(true);
          void startStorageClass({ path: "", whole_vault: true, target_storage_class: vaultTarget })
            .then((result) => onNotice(result.message || t("api.storage_class_started")))
            .catch((error: unknown) => onNotice(error instanceof Error ? error.message : String(error), true))
            .finally(() => setBusy(false));
        }}
      />
    </section>
  );
}

import { useEffect, useState } from "react";
import { X } from "lucide-react";

import {
  fetchOperationPolicy,
  previewOperationGlobs,
  updateOperationPolicy,
  type OperationPolicy,
} from "@/api";
import { FormField, FormInput } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

type OperationPolicyPanelProps = {
  onNotice: (message: string, error?: boolean) => void;
};

type Translate = (key: string, params?: Record<string, string | number>) => string;

function normalizePolicyPath(path: string): string {
  return path.trim().replaceAll("\\", "/").replace(/^\/+/, "");
}

function listHasPath(list: string[] | undefined, path: string): boolean {
  if (!Array.isArray(list)) return false;
  const normalized = normalizePolicyPath(path);
  return list.some((item) => normalizePolicyPath(item) === normalized);
}

function addRule(rules: string[], raw: string): string[] {
  const rule = raw.trim();
  if (!rule || rules.includes(rule)) return rules;
  return [...rules, rule];
}

function RuleList({
  id,
  title,
  help,
  emptyLabel,
  addLabel,
  rules,
  draft,
  onDraftChange,
  onAdd,
  onRemove,
  t,
}: {
  id: string;
  title: string;
  help: string;
  emptyLabel: string;
  addLabel: string;
  rules: string[];
  draft: string;
  onDraftChange: (value: string) => void;
  onAdd: () => void;
  onRemove: (rule: string) => void;
  t: Translate;
}) {
  return (
    <div className="rounded-[14px] border border-line bg-canvas p-3">
      <h3 className="text-sm font-bold text-ink">{title}</h3>
      <p className="mt-1 text-sm text-muted">{help}</p>
      <div className="mt-3 flex flex-wrap gap-2" data-testid={`${id}-rules`}>
        {rules.length === 0 ? (
          <p className="text-sm font-bold text-ink">{emptyLabel}</p>
        ) : (
          rules.map((rule) => (
            <span
              key={rule}
              className="inline-flex max-w-full items-center gap-1 rounded-badge border border-line bg-surface py-1 pr-1 pl-2.5 font-mono text-xs font-bold text-ink"
            >
              <span className="truncate">{rule}</span>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="size-7 min-h-7 min-w-7 rounded-full"
                aria-label={t("access.policy_remove_rule", { rule })}
                onClick={() => onRemove(rule)}
              >
                <X className="size-3.5" aria-hidden="true" />
              </Button>
            </span>
          ))
        )}
      </div>
      <div className="mt-3 flex flex-col gap-2 sm:flex-row">
        <FormField
          label={addLabel}
          htmlFor={`${id}-new`}
          className="min-w-0 flex-1"
        >
          <FormInput
            id={`${id}-new`}
            value={draft}
            placeholder={t("access.policy_rule_placeholder")}
            onChange={(event) => onDraftChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                onAdd();
              }
            }}
          />
        </FormField>
        <Button
          type="button"
          variant="secondary"
          className="min-h-11 sm:self-end"
          onClick={onAdd}
        >
          {t("access.policy_add_rule")}
        </Button>
      </div>
    </div>
  );
}

export function OperationPolicyPanel({ onNotice }: OperationPolicyPanelProps) {
  const { t, ready } = useI18n();
  const [loadState, setLoadState] = useState("");
  const [policy, setPolicy] = useState<OperationPolicy | null>(null);
  const [includeRules, setIncludeRules] = useState<string[]>([]);
  const [excludeRules, setExcludeRules] = useState<string[]>([]);
  const [includeDraft, setIncludeDraft] = useState("");
  const [excludeDraft, setExcludeDraft] = useState("");
  const [previewPath, setPreviewPath] = useState("");
  const [preview, setPreview] = useState<{
    path: string;
    included: boolean;
    reason: string;
  } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!ready) return;
    setLoadState(t("access.policy_loading"));
    void (async () => {
      try {
        const data = await fetchOperationPolicy();
        setPolicy(data);
        setIncludeRules(data.include_globs);
        setExcludeRules(data.exclude_globs);
        setLoadState(t("access.policy_loaded"));
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setLoadState(message);
        onNotice(message, true);
      }
    })();
  }, [onNotice, ready, t]);

  async function onPreview(event: React.FormEvent) {
    event.preventDefault();
    const path = normalizePolicyPath(previewPath.split(/\s+/)[0] ?? "");
    if (!path) {
      onNotice(t("access.policy_preview_empty"), true);
      return;
    }
    setBusy(true);
    try {
      const withExcludes = await previewOperationGlobs({
        paths: [path],
        include_globs: includeRules,
        exclude_globs: excludeRules,
      });
      const included = listHasPath(withExcludes.included, path);
      if (included) {
        setPreview({
          path,
          included: true,
          reason: t("access.policy_preview_would_upload"),
        });
        return;
      }
      if (excludeRules.length) {
        const withoutExcludes = await previewOperationGlobs({
          paths: [path],
          include_globs: includeRules,
          exclude_globs: [],
        });
        if (listHasPath(withoutExcludes.included, path)) {
          setPreview({
            path,
            included: false,
            reason: t("access.policy_preview_skipped_exclude"),
          });
          return;
        }
      }
      setPreview({
        path,
        included: false,
        reason: t("access.policy_preview_skipped_include"),
      });
    } catch (error) {
      onNotice(error instanceof Error ? error.message : String(error), true);
    } finally {
      setBusy(false);
    }
  }

  async function onSave(event: React.FormEvent) {
    event.preventDefault();
    if (!policy) return;
    setBusy(true);
    try {
      const updated = await updateOperationPolicy({
        ...policy,
        include_globs: includeRules,
        exclude_globs: excludeRules,
      });
      setPolicy(updated);
      setIncludeRules(updated.include_globs);
      setExcludeRules(updated.exclude_globs);
      onNotice(t("access.policy_saved"));
    } catch (error) {
      onNotice(error instanceof Error ? error.message : String(error), true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section data-panel="operation-policy">
      <Panel className="p-4 sm:p-5">
        <h2 className="text-lg font-bold">{t("access.policy_title")}</h2>
        <p className="mt-1 text-sm text-muted">{t("access.policy_help")}</p>
        <p className="mt-2 text-sm text-muted" role="status">
          {loadState}
        </p>

        <form className="mt-4 grid gap-3" onSubmit={(event) => void onSave(event)}>
          <RuleList
            id="include-globs"
            title={t("access.policy_include_globs")}
            help={t("access.policy_include_help")}
            emptyLabel={t("access.policy_include_empty")}
            addLabel={t("access.policy_new_include")}
            rules={includeRules}
            draft={includeDraft}
            onDraftChange={setIncludeDraft}
            onAdd={() => {
              setIncludeRules((current) => addRule(current, includeDraft));
              setIncludeDraft("");
            }}
            onRemove={(rule) =>
              setIncludeRules((current) => current.filter((item) => item !== rule))
            }
            t={t}
          />
          <RuleList
            id="exclude-globs"
            title={t("access.policy_exclude_globs")}
            help={t("access.policy_exclude_help")}
            emptyLabel={t("access.policy_exclude_empty")}
            addLabel={t("access.policy_new_exclude")}
            rules={excludeRules}
            draft={excludeDraft}
            onDraftChange={setExcludeDraft}
            onAdd={() => {
              setExcludeRules((current) => addRule(current, excludeDraft));
              setExcludeDraft("");
            }}
            onRemove={(rule) =>
              setExcludeRules((current) => current.filter((item) => item !== rule))
            }
            t={t}
          />
          <Button type="submit" className="min-h-11 w-full sm:w-auto" disabled={busy || !policy}>
            {t("access.policy_save")}
          </Button>
        </form>

        <form
          className="mt-6 grid gap-3 border-t border-line pt-4"
          onSubmit={(event) => void onPreview(event)}
        >
          <h3 className="text-sm font-bold text-ink">{t("access.policy_preview_title")}</h3>
          <p className="text-sm text-muted">{t("access.policy_preview_help")}</p>
          <div className="flex flex-col gap-2 sm:flex-row">
            <FormField
              label={t("access.policy_preview_path")}
              htmlFor="preview-path"
              className="min-w-0 flex-1"
            >
              <FormInput
                id="preview-path"
                value={previewPath}
                placeholder={t("access.policy_preview_placeholder")}
                onChange={(event) => setPreviewPath(event.target.value)}
              />
            </FormField>
            <Button
              type="submit"
              variant="secondary"
              className="min-h-11 sm:self-end"
              disabled={busy}
            >
              {t("access.policy_preview")}
            </Button>
          </div>
        </form>

        {preview ? (
          <div
            className={
              preview.included
                ? "mt-4 rounded-[14px] bg-green-soft px-3 py-3 text-sm font-bold text-ink"
                : "mt-4 rounded-[14px] bg-amber-soft px-3 py-3 text-sm font-bold text-ink"
            }
            data-testid="glob-preview"
          >
            <p className="font-mono text-xs font-medium text-muted">{preview.path}</p>
            <p className="mt-1">{preview.reason}</p>
          </div>
        ) : null}
      </Panel>
    </section>
  );
}

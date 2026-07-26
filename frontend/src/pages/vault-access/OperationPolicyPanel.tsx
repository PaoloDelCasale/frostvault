import { useEffect, useState } from "react";

import {
  fetchOperationPolicy,
  previewOperationGlobs,
  updateOperationPolicy,
  type GlobPreviewResponse,
  type OperationPolicy,
} from "@/api";
import { FormField } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

type OperationPolicyPanelProps = {
  onNotice: (message: string, error?: boolean) => void;
};

function linesToGlobs(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function globsToText(globs: string[]): string {
  return globs.join("\n");
}

export function OperationPolicyPanel({ onNotice }: OperationPolicyPanelProps) {
  const { t, ready } = useI18n();
  const [loadState, setLoadState] = useState("");
  const [policy, setPolicy] = useState<OperationPolicy | null>(null);
  const [includeText, setIncludeText] = useState("");
  const [excludeText, setExcludeText] = useState("");
  const [samplePaths, setSamplePaths] = useState("");
  const [preview, setPreview] = useState<GlobPreviewResponse | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!ready) return;
    setLoadState(t("access.policy_loading"));
    void (async () => {
      try {
        const data = await fetchOperationPolicy();
        setPolicy(data);
        setIncludeText(globsToText(data.include_globs));
        setExcludeText(globsToText(data.exclude_globs));
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
    const paths = linesToGlobs(samplePaths);
    if (!paths.length) {
      onNotice(t("access.policy_preview_empty"), true);
      return;
    }
    setBusy(true);
    try {
      const result = await previewOperationGlobs({
        paths,
        include_globs: linesToGlobs(includeText),
        exclude_globs: linesToGlobs(excludeText),
      });
      setPreview(result);
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
        include_globs: linesToGlobs(includeText),
        exclude_globs: linesToGlobs(excludeText),
      });
      setPolicy(updated);
      setIncludeText(globsToText(updated.include_globs));
      setExcludeText(globsToText(updated.exclude_globs));
      onNotice(t("access.policy_saved"));
    } catch (error) {
      onNotice(error instanceof Error ? error.message : String(error), true);
    } finally {
      setBusy(false);
    }
  }

  const textareaClass =
    "min-h-28 w-full rounded-[10px] border border-input bg-white px-3.5 py-[11px] font-mono text-sm text-ink";

  return (
    <section data-panel="operation-policy">
      <Panel className="p-4 sm:p-5">
        <h2 className="text-lg font-bold">{t("access.policy_title")}</h2>
        <p className="mt-1 text-sm text-muted">{t("access.policy_help")}</p>
        <p className="mt-2 text-sm text-muted" role="status">
          {loadState}
        </p>

        <form className="mt-4 grid gap-3" onSubmit={(event) => void onSave(event)}>
          <FormField
            label={t("access.policy_include_globs")}
            htmlFor="include-globs"
            help={t("access.policy_globs_help")}
          >
            <textarea
              id="include-globs"
              className={textareaClass}
              value={includeText}
              onChange={(event) => setIncludeText(event.target.value)}
            />
          </FormField>
          <FormField
            label={t("access.policy_exclude_globs")}
            htmlFor="exclude-globs"
            help={t("access.policy_globs_help")}
          >
            <textarea
              id="exclude-globs"
              className={textareaClass}
              value={excludeText}
              onChange={(event) => setExcludeText(event.target.value)}
            />
          </FormField>
          <Button type="submit" className="min-h-11 w-full sm:w-auto" disabled={busy || !policy}>
            {t("access.policy_save")}
          </Button>
        </form>

        <form className="mt-6 grid gap-3 border-t border-line pt-4" onSubmit={(event) => void onPreview(event)}>
          <FormField
            label={t("access.policy_preview_paths")}
            htmlFor="preview-paths"
            help={t("access.policy_globs_help")}
          >
            <textarea
              id="preview-paths"
              className={textareaClass}
              value={samplePaths}
              onChange={(event) => setSamplePaths(event.target.value)}
            />
          </FormField>
          <Button type="submit" variant="secondary" className="min-h-11 w-full sm:w-auto" disabled={busy}>
            {t("access.policy_preview")}
          </Button>
        </form>

        {preview ? (
          <div className="mt-4 grid gap-3 text-sm" data-testid="glob-preview">
            <div>
              <h3 className="font-bold">{t("access.policy_preview_included")}</h3>
              <ul className="mt-1 list-disc pl-5">
                {preview.included.map((path) => (
                  <li key={`in-${path}`}>{path}</li>
                ))}
              </ul>
            </div>
            <div>
              <h3 className="font-bold">{t("access.policy_preview_excluded")}</h3>
              <ul className="mt-1 list-disc pl-5">
                {preview.excluded.map((path) => (
                  <li key={`ex-${path}`}>{path}</li>
                ))}
              </ul>
            </div>
          </div>
        ) : null}
      </Panel>
    </section>
  );
}

import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { vaultApi, type GlobPreviewResponse, type OperationPolicy, type OperationPolicyGlobEdit } from "@/api";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/lib/errorMessage";

type Field = OperationPolicyGlobEdit["field"];
const isFolderRule = (rule: string) => rule.endsWith("/**");

/** One editable list of rules: each rule is added or removed on its own, against the stored policy (#754). */
function RuleList({ field, rules, busy, onAdd, onRemove }: {
  field: Field; rules: string[]; busy: boolean; onAdd: (rule: string) => Promise<boolean>; onRemove: (rule: string) => void;
}) {
  const { t } = useI18n();
  const [draft, setDraft] = useState("");
  const name = field === "exclude_globs" ? "exclude" : "include";
  async function add() {
    const rule = draft.trim();
    if (!rule || busy) return;
    if (await onAdd(rule)) setDraft("");
  }
  return <div className="grid gap-2" data-testid={`${name}-rules`}>
    {rules.length === 0 ? <p className="text-sm text-muted">{t("exclusions.none")}</p>
      : <ul className="flex flex-wrap gap-2">{rules.map((rule) => <li key={rule} className="inline-flex min-w-0 items-center gap-1 rounded-badge border border-line bg-canvas/60 pl-3 text-sm">
        <code className="break-all">{rule}</code>
        <Button variant="ghost" className="min-h-11 min-w-11 px-2" disabled={busy} aria-label={t("exclusions.remove", { rule })} onClick={() => onRemove(rule)}><X aria-hidden="true" /></Button>
      </li>)}</ul>}
    <form className="flex min-w-0 flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); void add(); }}>
      <label className="min-w-0 flex-1 basis-56"><span className="sr-only">{t(`exclusions.${name}_add_label`)}</span>
        <input className="min-h-11 w-full min-w-0 rounded-lg border border-input bg-surface px-3 font-mono text-sm" value={draft} disabled={busy}
          placeholder={t(`exclusions.${name}_placeholder`)} onChange={(event) => setDraft(event.target.value)} /></label>
      <Button type="submit" variant="secondary" disabled={busy || !draft.trim()}>{t("exclusions.add")}</Button>
    </form>
  </div>;
}

/** «Esclusioni» in the Vault settings: the rules, the fixed noise exclusions and a preview (#754). */
export function ExclusionsPanel({ vaultId, onNotice }: { vaultId: number; onNotice: (message: string, error?: boolean) => void }) {
  const { t } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const [policy, setPolicy] = useState<OperationPolicy | null>(null);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState<{ field: Field; rule: string } | null>(null);
  const [paths, setPaths] = useState("");
  const [preview, setPreview] = useState<GlobPreviewResponse | null>(null);
  useEffect(() => {
    let live = true;
    setPolicy(null); setLoadError(""); setPreview(null);
    void vaultApi(vaultId).policy().then((data) => { if (live) setPolicy(data); })
      .catch((error: unknown) => { if (live) setLoadError(errorMessage(error, translateRef.current)); });
    return () => { live = false; };
  }, [vaultId]);
  async function edit(field: Field, action: "add" | "remove", rule: string): Promise<boolean> {
    setBusy(true);
    try {
      setPolicy(await vaultApi(vaultId).editGlob({ field, action, glob: rule }));
      setPreview(null);
      onNotice(t(action === "add" ? "exclusions.added" : "exclusions.removed", { rule }));
      return true;
    } catch (error) {
      onNotice(errorMessage(error, t), true);
      // A 409 means the view was stale: show the stored rules again.
      void vaultApi(vaultId).policy().then(setPolicy).catch(() => undefined);
      return false;
    } finally { setBusy(false); }
  }
  async function runPreview() {
    if (!policy) return;
    const list = paths.split("\n").map((line) => line.trim()).filter(Boolean);
    if (!list.length) return;
    try { setPreview(await vaultApi(vaultId).previewGlobs({ paths: list, include_globs: policy.include_globs ?? [], exclude_globs: policy.exclude_globs ?? [] })); }
    catch (error) { onNotice(errorMessage(error, t), true); }
  }
  const removeDescription = removing ? removing.field === "include_globs" ? t("exclusions.confirm_remove_include", { rule: removing.rule })
    : `${t("exclusions.confirm_remove_exclude", { rule: removing.rule })}${isFolderRule(removing.rule) ? `\n\n${t("exclusions.remove_folder_scan")}` : ""}` : "";
  return <div className="grid gap-5" data-testid="exclusions-panel">
    {loadError && <p role="alert" className="text-sm text-destructive">{loadError}</p>}
    <div className="grid gap-2 text-sm">
      <p>{t("exclusions.folder_rule_help")}</p>
      <p>{t("exclusions.cloud_kept")}</p>
      <p className="text-muted">{t("exclusions.system_folders")}</p>
    </div>
    <section className="grid gap-2" aria-labelledby={`exclude-rules-${vaultId}`}>
      <h3 id={`exclude-rules-${vaultId}`} className="font-semibold">{t("exclusions.exclude_title")}</h3>
      {policy && <RuleList field="exclude_globs" rules={policy.exclude_globs ?? []} busy={busy} onAdd={(rule) => edit("exclude_globs", "add", rule)} onRemove={(rule) => setRemoving({ field: "exclude_globs", rule })} />}
    </section>
    <section className="grid gap-2" aria-labelledby={`include-rules-${vaultId}`}>
      <h3 id={`include-rules-${vaultId}`} className="font-semibold">{t("exclusions.include_title")}</h3>
      <p className="text-sm text-muted">{t("exclusions.include_help")}</p>
      <p className="text-sm text-muted" data-testid="exclusions-order-note">{t("exclusions.order_note")}</p>
      {policy && <RuleList field="include_globs" rules={policy.include_globs ?? []} busy={busy} onAdd={(rule) => edit("include_globs", "add", rule)} onRemove={(rule) => setRemoving({ field: "include_globs", rule })} />}
    </section>
    <section className="grid gap-2" aria-labelledby={`fixed-rules-${vaultId}`}>
      <h3 id={`fixed-rules-${vaultId}`} className="font-semibold">{t("exclusions.fixed_title")}</h3>
      <p className="text-sm text-muted">{t("exclusions.fixed_help")}</p>
      <ul className="flex flex-wrap gap-2" data-testid="fixed-rules">{(policy?.fixed_exclude_globs ?? []).map((rule) => <li key={rule} className="rounded-badge border border-line px-3 py-1 text-sm"><code>{rule}</code></li>)}</ul>
    </section>
    <details className="rounded-card border border-line">
      <summary className="min-h-11 cursor-pointer px-3 py-3 font-semibold">{t("exclusions.preview_title")}</summary>
      <div className="grid gap-3 border-t border-line p-3">
        <label className="grid gap-1 text-sm font-semibold">{t("exclusions.preview_paths")}
          <textarea className="min-h-24 w-full min-w-0 rounded-lg border border-input bg-surface p-3 font-mono text-sm font-normal" value={paths} onChange={(event) => setPaths(event.target.value)} /></label>
        <Button variant="secondary" className="w-fit" disabled={!policy || !paths.trim()} onClick={() => void runPreview()}>{t("exclusions.preview_run")}</Button>
        {preview && <div className="grid gap-2 text-sm sm:grid-cols-2" data-testid="glob-preview">
          <div><p className="font-semibold">{t("exclusions.preview_included")}</p><ul>{preview.included.map((path) => <li key={path} className="break-all">{path}</li>)}</ul></div>
          <div><p className="font-semibold">{t("exclusions.preview_excluded")}</p><ul>{preview.excluded.map((path) => <li key={path} className="break-all">{path}</li>)}</ul></div>
        </div>}
      </div>
    </details>
    <ConfirmDialog open={Boolean(removing)} onOpenChange={(open) => { if (!open && !busy) setRemoving(null); }} tone="default"
      title={t("exclusions.confirm_remove_title")} description={removeDescription} confirmLabel={t("exclusions.remove_confirm")} cancelLabel={t("ui.cancel")}
      confirmDisabled={busy} onConfirm={() => { const target = removing; setRemoving(null); if (target) void edit(target.field, "remove", target.rule); }} />
  </div>;
}

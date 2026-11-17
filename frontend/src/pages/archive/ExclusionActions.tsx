import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { DropdownMenu } from "radix-ui";
import { MoreHorizontal } from "lucide-react";
import { vaultApi } from "@/api/vault";
import type { VaultInclusionPreview } from "@/api/types";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/errorMessage";
import { navigate, vaultSettingsUrl } from "@/router";
import { formatBytes, formatEur } from "./format";
import { EXCLUSIONS_SECTION, hasGlobCharacters, ownRule } from "./exclusionRules";

type Translate = (key: string, params?: Record<string, string | number>) => string;

/** A file or folder the Owner can exclude from, or include again in, the Vault (#754). */
export type ExclusionTarget = { path: string; name: string; isDirectory: boolean; excluded: boolean };


const itemClass = "flex min-h-11 cursor-pointer items-center rounded px-3 text-sm outline-none data-[disabled]:cursor-not-allowed data-[disabled]:text-muted data-[highlighted]:bg-green-soft";

/** «Escludi dal vault» / «Includi di nuovo» on one row of the Vault browser. */
export function ExclusionMenu({ target, t, vaultId, onExclude, onInclude }: {
  target: ExclusionTarget; t: Translate; vaultId: number; onExclude: () => void; onInclude: () => void;
}) {
  const globName = hasGlobCharacters(target.path);
  // A rule without "/" would match the name in every folder (#754).
  const rootFile = !target.isDirectory && !target.path.includes("/");
  const blocked = !target.excluded && (globName || rootFile);
  return <DropdownMenu.Root>
    <DropdownMenu.Trigger asChild>
      <Button variant="ghost" className="min-h-11 min-w-11 px-2" aria-label={t("exclusions.actions", { name: target.name })} data-testid="exclusion-menu">
        <MoreHorizontal aria-hidden="true" />
      </Button>
    </DropdownMenu.Trigger>
    <DropdownMenu.Portal>
      <DropdownMenu.Content align="end" sideOffset={4} collisionPadding={8} className="z-50 grid w-[min(20rem,calc(100vw-1rem))] gap-1 rounded-card border border-line bg-surface p-1 text-ink shadow-lg">
        {target.excluded
          ? <DropdownMenu.Item className={itemClass} onSelect={onInclude}>{t("exclusions.include")}</DropdownMenu.Item>
          : <DropdownMenu.Item className={itemClass} disabled={blocked} onSelect={onExclude}>{t("exclusions.exclude")}</DropdownMenu.Item>}
        {blocked && <p className="px-3 pb-1 text-xs text-muted" data-testid="exclude-glob-hint">{t(globName ? "exclusions.exclude_glob_disabled" : "exclusions.exclude_root_file_disabled")}</p>}
        <DropdownMenu.Item className={itemClass} onSelect={() => navigate(vaultSettingsUrl(vaultId, EXCLUSIONS_SECTION))}>{t("exclusions.open_panel")}</DropdownMenu.Item>
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  </DropdownMenu.Root>;
}

/** Confirms «Escludi dal vault»: nothing is deleted, later changes are not uploaded. */
export function ExcludeDialog({ vaultId, target, t, onClose, onDone }: {
  vaultId: number; target: ExclusionTarget; t: Translate; onClose: () => void; onDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const rule = ownRule(target.path, target.isDirectory);
  async function confirm() {
    if (busy) return;
    setBusy(true); setError("");
    try { await vaultApi(vaultId).exclude(target.path, target.isDirectory); onDone(); }
    catch (cause) { setError(errorMessage(cause, t)); setBusy(false); }
  }
  return <ConfirmDialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }} tone="default" keepOpenOnConfirm confirmDisabled={busy}
    title={t("exclusions.confirm_exclude_title", { name: target.name })}
    description={t(target.isDirectory ? "exclusions.confirm_exclude_folder" : "exclusions.confirm_exclude_file", { rule })}
    confirmLabel={t("exclusions.exclude")} cancelLabel={t("ui.cancel")} onConfirm={() => void confirm()}>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
  </ConfirmDialog>;
}

function quantity(preview: VaultInclusionPreview, t: Translate): string {
  if (preview.files === null || preview.bytes === null) return t("exclusions.include_unknown");
  const values = { count: preview.files, size: formatBytes(preview.bytes) };
  return t(preview.complete === false ? "exclusions.include_upload_minimum" : "exclusions.include_upload", values);
}

/** «Includi di nuovo»: removes the item's own rule, or names the broader rule and points to the panel. */
export function IncludeDialog({ vaultId, target, t, onClose, onDone }: {
  vaultId: number; target: ExclusionTarget; t: Translate; onClose: () => void; onDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const preview = useQuery({
    queryKey: ["inclusion-preview", vaultId, target.path, target.isDirectory],
    queryFn: () => vaultApi(vaultId).inclusionPreview(target.path, target.isDirectory),
    retry: false, gcTime: 0,
  });
  const data = preview.data;
  const toPanel = () => { onClose(); navigate(vaultSettingsUrl(vaultId, EXCLUSIONS_SECTION)); };
  async function confirm() {
    if (!data) return;
    if (!data.includable) { toPanel(); return; }
    if (busy) return;
    setBusy(true); setError("");
    try { await vaultApi(vaultId).include(target.path, target.isDirectory); onDone(); }
    catch (cause) { setError(errorMessage(cause, t)); setBusy(false); }
  }
  const remaining = data ? [...data.rules, ...data.fixed] : [];
  const description = !data ? (preview.isError ? errorMessage(preview.error, t) : t("exclusions.loading"))
    : data.includable ? t("exclusions.include_removes", { rule: data.own_rule ?? "" })
      : data.fixed.length && !data.rules.length ? t("exclusions.fixed_reason", { name: target.name, rules: data.fixed.join(", ") })
        : data.rules.length ? t("exclusions.broader", { name: target.name, rules: data.rules.join(", ") })
          : data.not_included ? t("exclusions.not_included", { name: target.name })
            : t("exclusions.not_excluded", { name: target.name });
  return <ConfirmDialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }} tone="default" keepOpenOnConfirm
    confirmDisabled={busy || !data || (!data.includable && Boolean(data.fixed.length && !data.rules.length && !data.not_included))}
    title={t(data && !data.includable ? "exclusions.broader_title" : "exclusions.confirm_include_title", { name: target.name })}
    description={description} cancelLabel={t("ui.cancel")} onConfirm={() => void confirm()}
    confirmLabel={data && !data.includable ? t("exclusions.open_panel") : t("exclusions.include")}>
    {data?.includable && <div className="grid gap-2 text-sm" data-testid="inclusion-preview">
      <p className="font-semibold">{quantity(data, t)}</p>
      {data.estimate && <p>{t("exclusions.include_cost", { cost: formatEur(data.estimate.estimated_cost_eur) })}</p>}
      {data.folder_rule && <p>{t("exclusions.remove_folder_scan")}</p>}
      {remaining.length > 0 && <p>{t("exclusions.include_still", { rules: remaining.join(", ") })}</p>}
      {data.not_included && <p>{t("exclusions.not_included", { name: target.name })}</p>}
      {target.isDirectory && <p className="text-muted">{t("exclusions.system_folders")}</p>}
    </div>}
    {data && !data.includable && <p className="text-sm text-muted">{t("exclusions.order_note")}</p>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {preview.isError && <Button variant="secondary" className="w-fit" onClick={() => void preview.refetch()}>{t("ui.file_list_retry")}</Button>}
  </ConfirmDialog>;
}

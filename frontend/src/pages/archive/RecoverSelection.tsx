import { useEffect, useState } from "react";
import type { ArchiveListItem, RecoverEstimateResponse } from "@/api/types";
import { vaultApi } from "@/api/vault";
import { Dialog } from "@/components/Dialog";
import { Button } from "@/components/ui/button";

export type Target = { item: ArchiveListItem; vaultId: number; vaultName: string };
type Preview = { target: Target; estimate: RecoverEstimateResponse; retention: number | null };
export function RecoverSelection({ targets, onClose, onDone, t }: { targets: Target[]; onClose: () => void; onDone: () => void; t: (key: string, params?: Record<string, string | number>) => string }) {
  const [tier, setTier] = useState("Bulk");
  const [previews, setPreviews] = useState<Preview[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [completed, setCompleted] = useState<Set<string>>(new Set());
  useEffect(() => {
    let live = true;
    setPreviews(null); setError("");
    void Promise.all(targets.map(async (target) => {
      const api = vaultApi(target.vaultId);
      const [estimate, policy] = await Promise.all([api.estimate({ path: target.item.path, is_directory: target.item.type === "directory", restore_tier: tier }), api.policy()]);
      return { target, estimate, retention: policy.local_release_after_days ?? null };
    })).then((data) => { if (live) setPreviews(data); }).catch((cause: unknown) => { if (live) setError(String(cause)); });
    return () => { live = false; };
  }, [targets, tier]);
  async function recover() {
    if (!previews || busy) return;
    setBusy(true); setError("");
    try {
      for (const { target } of previews) {
        const key = `${target.vaultId}:${target.item.path}`;
        if (completed.has(key)) continue;
        await vaultApi(target.vaultId).action("recover", { path: target.item.path, is_directory: target.item.type === "directory", restore_tier: tier });
        completed.add(key); setCompleted(new Set(completed));
      }
      onDone();
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  }
  const cold = previews?.some((row) => row.estimate.requires_restore);
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }} title={t("ui.row_action_recover")} description={t("essential.recover_help")}>
    <div className="grid gap-4">
      {cold && <label className="grid gap-2">{t("essential.recover_speed")}<select className="min-h-11 rounded border border-input bg-surface px-3" value={tier} disabled={busy} onChange={(event) => setTier(event.target.value)}><option value="Bulk">{t("essential.bulk")}</option><option value="Standard">{t("essential.standard")}</option></select></label>}
      {!previews && !error && <p role="status">{t("ui.loading")}</p>}
      {previews?.map(({ target, estimate, retention }) => <div key={`${target.vaultId}:${target.item.path}`} className="break-words border-t border-line pt-3"><strong>{target.vaultName} / {target.item.path}</strong><p>{t("essential.return_path", { path: `${target.vaultName} / ${target.item.path}` })}</p><p>{retention ? t("essential.recovered_retention", { days: retention }) : t("essential.recovered_keep")}</p><p>{estimate.requires_restore && estimate.estimate ? t("essential.recover_estimate", { cost: Number(estimate.estimate.estimated_cost_eur).toFixed(2), hours: estimate.estimate.estimated_hours }) : t("essential.ready_now")}</p></div>)}
      {cold && <p>{t("ui.recover_irreversible_note")}</p>}
      <p className="text-sm text-muted">{t("essential.estimate_caveat")}</p><p>{t("essential.recover_notification")}</p>
      {error && <p role="alert">{error}</p>}
      <div className="flex flex-wrap gap-2"><Button variant="secondary" disabled={busy} onClick={onClose}>{t("ui.cancel")}</Button><Button disabled={busy || !previews} onClick={() => void recover()}>{busy ? t("ui.loading") : t("ui.row_action_recover")}</Button></div>
    </div>
  </Dialog>;
}

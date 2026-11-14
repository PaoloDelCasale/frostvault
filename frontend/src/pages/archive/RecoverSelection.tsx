import { useEffect, useState } from "react";
import type { ArchiveListItem, RecoverEstimateResponse } from "@/api/types";
import { vaultApi } from "@/api/vault";
import { Dialog } from "@/components/Dialog";
import { Button } from "@/components/ui/button";

export type Target = { item: ArchiveListItem; vaultId: number; vaultName: string };
type Preview = { targets: Target[]; estimate: RecoverEstimateResponse; retention: number | null };

// A Vault is an authorization/storage boundary: one request per Vault, never per file.
function groupByVault(targets: Target[]): Target[][] {
  const groups = new Map<number, Target[]>();
  for (const target of targets) {
    const group = groups.get(target.vaultId) ?? [];
    group.push(target); groups.set(target.vaultId, group);
  }
  return [...groups.values()];
}
function requestScope(targets: Target[]) {
  const selection = targets.map(({ item }) => ({ path: item.path, is_directory: item.type === "directory" }));
  return selection.length === 1 ? selection[0] : { path: "", selection };
}
export function RecoverSelection({ targets, onClose, onDone, t }: { targets: Target[]; onClose: () => void; onDone: () => void; t: (key: string, params?: Record<string, string | number>) => string }) {
  const [tier, setTier] = useState("Bulk");
  const [previews, setPreviews] = useState<Preview[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [completed, setCompleted] = useState<Set<string>>(new Set());
  useEffect(() => {
    let live = true;
    setPreviews(null); setError("");
    void Promise.all(groupByVault(targets).map(async (group) => {
      const api = vaultApi(group[0].vaultId);
      const [estimate, policy] = await Promise.all([api.estimate({ ...requestScope(group), restore_tier: tier }), api.policy()]);
      return { targets: group, estimate, retention: policy.local_release_after_days ?? null };
    })).then((data) => { if (live) setPreviews(data); }).catch((cause: unknown) => { if (live) setError(String(cause)); });
    return () => { live = false; };
  }, [targets, tier]);
  async function recover() {
    if (!previews || busy) return;
    setBusy(true); setError("");
    try {
      for (const { targets: group, estimate } of previews) {
        const key = String(group[0].vaultId);
        if (completed.has(key)) continue;
        await vaultApi(group[0].vaultId).action("recover", { ...requestScope(group), restore_tier: tier,
          ...(estimate.archive_version_id ? { archive_version_id: estimate.archive_version_id } : {}),
          ...(estimate.estimate?.days ? { restore_days: estimate.estimate.days } : {}),
        });
        completed.add(key); setCompleted(new Set(completed));
      }
      onDone();
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  }
  const cold = previews?.some((row) => row.estimate.requires_restore);
  // Restore Approval is decided per request: each held request waits in Active operations (#533).
  const needsApproval = previews?.some((row) => row.estimate.high_impact);
  const totalCost = previews?.reduce((sum, row) => sum + (row.estimate.requires_restore && row.estimate.estimate ? Number(row.estimate.estimate.estimated_cost_eur) : 0), 0) ?? 0;
  const maxHours = previews?.reduce((max, row) => Math.max(max, row.estimate.requires_restore && row.estimate.estimate ? Number(row.estimate.estimate.estimated_hours) : 0), 0) ?? 0;
  const rows = previews?.map(({ targets: group, estimate, retention }) => <div key={group[0].vaultId} className="break-words border-t border-line pt-3"><strong>{group[0].vaultName}</strong>{group.map((target, index) => <p key={index}>{t("essential.return_path", { path: `${target.vaultName} / ${target.item.path}` })}</p>)}<p>{retention ? t("essential.recovered_retention", { days: retention }) : t("essential.recovered_keep")}</p><p>{estimate.requires_restore && estimate.estimate ? t("essential.recover_estimate", { cost: Number(estimate.estimate.estimated_cost_eur).toFixed(2), hours: estimate.estimate.estimated_hours }) : t("essential.ready_now")}</p>{estimate.high_impact && <p className="font-semibold" data-testid="recover-needs-approval">{t("essential.recover_needs_approval")}</p>}</div>);
  const actions = <><Button variant="secondary" disabled={busy} onClick={onClose}>{t("ui.cancel")}</Button><Button disabled={busy || !previews} onClick={() => void recover()}>{busy ? t("ui.loading") : t("ui.row_action_recover")}</Button></>;
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }} title={t("ui.row_action_recover")} closeLabel={t("ui.close")} description={t("essential.recover_help")} footer={actions}>
    <div className="grid gap-4">
      {cold && <label className="grid gap-2">{t("essential.recover_speed")}<select className="min-h-11 rounded border border-input bg-surface px-3" value={tier} disabled={busy} onChange={(event) => setTier(event.target.value)}><option value="Bulk">{t("essential.bulk")}</option><option value="Standard">{t("essential.standard")}</option></select></label>}
      {!previews && !error && <p role="status">{t("ui.loading")}</p>}
      {previews && targets.length > 1 && <p className="font-semibold" data-testid="recover-summary">{t(cold ? "essential.recover_summary_cold" : "essential.recover_summary", { count: targets.length, cost: totalCost.toFixed(2), hours: maxHours })}</p>}
      {previews && targets.length > 3 ? <details><summary className="min-h-11 w-fit cursor-pointer py-2 font-semibold">{t("essential.recover_items")}</summary><div className="grid gap-3">{rows}</div></details> : rows}
      {needsApproval && <p role="note" className="font-semibold" data-testid="recover-approval-notice">{t("essential.recover_approval_notice")}</p>}
      {cold && <p>{t("ui.recover_irreversible_note")}</p>}
      <p className="text-sm text-muted">{t("essential.estimate_caveat")}</p><p>{t("essential.recover_notification")}</p>
      {error && <p role="alert">{error}</p>}
    </div>
  </Dialog>;
}

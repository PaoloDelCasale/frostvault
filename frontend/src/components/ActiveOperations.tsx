import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { vaultApi } from "@/api/vault";
import { Button } from "@/components/ui/button";

type Props = { vaultId: number; moves?: boolean; t: (key: string, params?: Record<string, string | number>) => string };

/** Active owner-requested work only: not a notification/event history. */
export function ActiveOperations(props: Props) {
  return <ActiveOperationsPage key={`${props.vaultId}:${Boolean(props.moves)}`} {...props} />;
}

function ActiveOperationsPage({ vaultId, moves = false, t }: Props) {
  const client = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const api = vaultApi(vaultId);
  const [pages, setPages] = useState<number[]>([]);
  const beforeId = pages.at(-1);
  const query = useQuery({ queryKey: ["jobs", vaultId, moves, beforeId], queryFn: () => api.jobs({ actions: moves ? ["storage-class"] : ["recover", "free-space"], activeOnly: true, groupsBeforeId: beforeId }), refetchInterval: 3_000 });
  const jobs = query.data?.groups?.filter((job) => !["completed", "cancelled", "failed"].includes(job.status) && (moves ? job.action === "storage-class" : ["recover", "free-space"].includes(job.action))) ?? [];
  async function mutate(group: string, action: string, approve = false) {
    if (busy) return;
    setBusy(group); setMessage("");
    try {
      await api.post(approve ? "/api/recover/approve" : "/api/jobs/cancel", { group_id: group, ...(approve ? {} : { action }) });
      if (!approve && action === "recover") setMessage(t("ui.operation_not_cancellable"));
      await Promise.all([client.invalidateQueries({ queryKey: ["jobs", vaultId] }), client.invalidateQueries({ queryKey: ["files"] })]);
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(null); }
  }
  if (!jobs.length && !message && !beforeId && !query.error) return null;
  return <section className="grid gap-3 rounded-panel border border-line p-3" aria-label={t("ui.active_operations")}>
    <h2 className="font-bold">{t("ui.active_operations")}</h2>
    {jobs.map((job) => <div key={job.id} className="grid gap-2 break-words text-sm"><strong>{t(moves ? "move.title" : `action.${job.action}`)} · {job.path}</strong><p>{t(`operation.${job.status}`)} · {job.completed_count}/{job.item_count} · {Math.round(job.percent)}%</p>
      {job.status === "pending_approval" && <><p>{t("essential.approval_cost", { cost: job.estimated_cost_eur == null ? "—" : Number(job.estimated_cost_eur).toFixed(2), hours: job.estimated_hours ?? "—" })}</p><Button disabled={busy !== null} onClick={() => void mutate(job.id, job.action, true)}>{t("ui.approve_restore")}</Button></>}
      <Button variant="secondary" disabled={busy !== null} onClick={() => void mutate(job.id, job.action)}>{t("ui.cancel")}</Button>
    </div>)}
    <div className="flex gap-2">
      {beforeId && <Button variant="secondary" onClick={() => setPages((value) => value.slice(0, -1))}>{t("ui.previous")}</Button>}
      {query.data?.next_groups_before_id && <Button variant="secondary" onClick={() => setPages((value) => [...value, query.data!.next_groups_before_id!])}>{t("ui.next")}</Button>}
    </div>
    {query.error && <p role="alert">{String(query.error)}</p>}
    {message && <p role="status">{message}</p>}
  </section>;
}

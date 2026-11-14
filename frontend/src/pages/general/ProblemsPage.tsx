import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "@/api/client";
import type { NotificationsResponse } from "@/api/endpoints";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { problemLink } from "@/lib/problemLink";
import { errorMessage } from "@/lib/errorMessage";
import { formatDateTime } from "@/pages/archive/format";

/** Names a problem's Vault; the list comes from the shell so no extra request is made. */
export function ProblemsPage({ vaults = [] }: { vaults?: readonly { id: number; name: string }[] }) {
  const { t } = useI18n();
  const [page, setPage] = useState(1);
  const vaultName = (id: number | null) => id ? vaults.find((vault) => vault.id === id)?.name ?? `#${id}` : null;
  const query = useQuery({ queryKey: ["problems", page], queryFn: () => apiRequest<NotificationsResponse>(`/api/problems?page=${page}`), refetchInterval: 30_000 });
  return <section className="grid gap-4"><h1 className="text-2xl font-bold">{t("essential.problems")}</h1>
    {query.isPending && <p role="status">{t("ui.loading")}</p>}{query.error && <div role="alert" className="grid justify-items-start gap-2" data-testid="problems-error"><p>{errorMessage(query.error, t)}</p><Button variant="secondary" disabled={query.isFetching} onClick={() => void query.refetch()}>{t("ui.retry")}</Button></div>}
    {query.data?.items.map((item) => <article key={item.id} className="grid gap-1 rounded-panel border border-line bg-surface p-4" data-testid="problem-row"><h2 className="font-bold">{item.title}</h2>
      <p className="flex flex-wrap gap-x-3 text-sm text-muted"><span>{item.vault_id ? t("essential.problem_vault", { name: vaultName(item.vault_id) ?? "" }) : t("essential.problem_system")}</span><time dateTime={item.created_at}>{formatDateTime(item.created_at)}</time></p>
      <p className="break-words">{item.body}</p><a className="inline-flex min-h-11 items-center font-bold underline" href={problemLink(item.vault_id, item.message_params)} aria-label={t("essential.open_problem_named", { title: item.title })}>{t("essential.open_problem")}</a></article>)}
    {query.data?.items.length === 0 && <p>{t("essential.no_problems")}</p>}
    <div className="flex gap-2"><Button variant="secondary" disabled={page === 1} onClick={() => setPage(page - 1)}>{t("ui.previous")}</Button><Button variant="secondary" disabled={(query.data?.items.length ?? 0) < 100} onClick={() => setPage(page + 1)}>{t("ui.next")}</Button></div>
  </section>;
}

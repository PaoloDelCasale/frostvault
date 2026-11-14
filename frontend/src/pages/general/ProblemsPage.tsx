import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "@/api/client";
import type { NotificationsResponse } from "@/api/endpoints";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { problemLink } from "@/lib/problemLink";
import { errorMessage } from "@/lib/errorMessage";

export function ProblemsPage() {
  const { t } = useI18n();
  const [page, setPage] = useState(1);
  const query = useQuery({ queryKey: ["problems", page], queryFn: () => apiRequest<NotificationsResponse>(`/api/problems?page=${page}`), refetchInterval: 30_000 });
  return <section className="grid gap-4"><h1 className="text-2xl font-bold">{t("essential.problems")}</h1>
    {query.isPending && <p role="status">{t("ui.loading")}</p>}{query.error && <p role="alert">{errorMessage(query.error, t)}</p>}
    {query.data?.items.map((item) => <article key={item.id} className="rounded-panel border border-line bg-surface p-4"><h2 className="font-bold">{item.title}</h2><p className="break-words">{item.body}</p><a className="inline-flex min-h-11 items-center font-bold underline" href={problemLink(item.vault_id, item.message_params)}>{t("essential.open_problem")}</a></article>)}
    {query.data?.items.length === 0 && <p>{t("essential.no_problems")}</p>}
    <div className="flex gap-2"><Button variant="secondary" disabled={page === 1} onClick={() => setPage(page - 1)}>{t("ui.previous")}</Button><Button variant="secondary" disabled={(query.data?.items.length ?? 0) < 100} onClick={() => setPage(page + 1)}>{t("ui.next")}</Button></div>
  </section>;
}

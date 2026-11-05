import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "@/api/client";
import type { FilesResponse } from "@/api/types";
import { vaultApi } from "@/api/vault";
import { Badge } from "@/components/Badge";
import { itemStateBadge } from "./protectionLabel";
import { Button } from "@/components/ui/button";
import { ActiveOperations } from "@/components/ActiveOperations";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { VaultSettingsPanel } from "@/pages/vault-settings/VaultSettingsPanel";
import { FileDetails } from "./FileDetails";
import { RecoverSelection, type Target } from "./RecoverSelection";
import { formatBytes } from "./format";

type Translate = (key: string, params?: Record<string, string | number>) => string;
export type FileBrowserProps = { t: Translate; vaultId: number; vaultName: string; capabilities: { delete_enabled: boolean; cloud_deletion_enabled: boolean }; problemCount?: number; onRefresh?: () => void };
export const SEARCH_DEBOUNCE_MS = 250;
export function FileBrowser({ t, vaultId, vaultName, problemCount = 0, onRefresh }: FileBrowserProps) {
  const [revision, setRevision] = useState(0);
  const [params, setParams] = useState(() => new URLSearchParams(window.location.search));
  const [input, setInput] = useState(params.get("q") ?? "");
  const [selected, setSelected] = useState<Target[]>([]);
  const [details, setDetails] = useState<Target | null>(null);
  const [recover, setRecover] = useState<Target[] | null>(null);
  const [release, setRelease] = useState<Target[] | null>(null);
  const [settings, setSettings] = useState(params.has("settings"));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const scope = useMemo(() => vaultApi(vaultId), [vaultId]);
  const q = params.get("q") ?? "", state = params.get("state") ?? "", directory = params.get("directory") ?? "";
  const page = Math.max(1, Number(params.get("page")) || 1);
  const global = Boolean(q);
  function update(values: Record<string, string>, push = false) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(values)) { if (value) next.set(key, value); else next.delete(key); }
    window.history[push ? "pushState" : "replaceState"]({}, "", `/?${next}`);
    setParams(next); setSelected([]); setDetails(null);
  }
  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (input === q) return;
      const next = new URLSearchParams(window.location.search);
      if (input) next.set("q", input); else next.delete("q");
      next.delete("page"); next.delete("file");
      window.history.replaceState({}, "", `/?${next}`); setParams(next); setSelected([]); setDetails(null);
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [input, q]);
  useEffect(() => {
    const pop = () => { const next = new URLSearchParams(window.location.search); setParams(next); setInput(next.get("q") ?? ""); setSelected([]); setDetails(null); };
    window.addEventListener("popstate", pop); return () => window.removeEventListener("popstate", pop);
  }, []);
  const query = useQuery({ queryKey: ["files", vaultId, q, state, directory, page, revision], queryFn: () => {
    const search = new URLSearchParams({ q, state, directory, page: String(page), page_size: "100" });
    return global ? apiRequest<FilesResponse>(`/api/files/search?${search}`) : scope.files(search);
  }, refetchInterval: 15_000 });
  const focusPath = params.get("file");
  const focus = useQuery({ queryKey: ["file-detail", vaultId, focusPath, revision], queryFn: () => scope.details(focusPath!), enabled: Boolean(focusPath) && !global });
  const items = [...(query.data?.items ?? [])];
  if (focus.data && !items.some((item) => item.path === focusPath)) items.unshift(focus.data);
  const rows = items.map((item): Target => ({ item, vaultId: typeof item.vault_id === "number" ? item.vault_id : vaultId, vaultName: typeof item.vault_name === "string" ? item.vault_name : vaultName }));
  const effectiveDetails = details ?? (focus.data ? { item: focus.data, vaultId, vaultName } : null);
  const refresh = () => { setSelected([]); setRevision((value) => value + 1); onRefresh?.(); };
  function openFolder(path: string) { setInput(""); update({ directory: path, q: "", page: "", file: "" }, true); }
  function eligible(target: Target, action: "recover" | "free-space") {
    const item = target.item;
    return item.type === "directory" ? Boolean(item.available_actions?.[action]) : action === "recover" ? Boolean(item.recover_eligible) : Boolean(item.cleanup_eligible);
  }
  async function free() {
    if (!release || busy) return;
    setBusy(true); setMessage("");
    try { for (const target of release) await vaultApi(target.vaultId).action("free-space", { path: target.item.path, is_directory: target.item.type === "directory" }); setRelease(null); refresh(); }
    catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }
  const columns = ["essential.name", "essential.protection", "essential.size", "essential.modified", "essential.ready"];
  const cell = (target: Target, column: number) => {
    const item = target.item;
    if (column === 0) return <button className="min-h-11 max-w-full break-words text-left font-semibold underline" onClick={() => {
      if (item.type === "directory") openFolder(item.path);
      else if (global) {
        const next = new URLSearchParams({ vault_id: String(target.vaultId), directory: item.path.includes("/") ? item.path.slice(0, item.path.lastIndexOf("/")) : "", file: item.path });
        window.location.assign(`/?${next}`);
      } else setDetails(target);
    }}>{item.type === "directory" ? "📁 " : ""}{global ? `${target.vaultName} / ${item.path}` : item.name}</button>;
    if (column === 1) return item.type === "directory" ? <span className="text-sm">{Object.entries(item.state_counts ?? {}).filter(([, count]) => count > 0).map(([status, count]) => `${count} ${t(`state.${status}`)}`).join(" · ") || "—"}</span> : <Badge {...itemStateBadge(item, t)} />;
    if (column === 2) return formatBytes(item.type === "directory" ? item.total_size : Number((item.local_exists ? item.local_size : item.cloud_size) ?? 0));
    if (column === 3) return typeof item.modified_at === "string" ? new Date(item.modified_at).toLocaleString() : "—";
    return item.type === "file" && item.state === "cloud_only" ? t(item.storage_class === "DEEP_ARCHIVE" ? "essential.ready_deep" : item.storage_class === "GLACIER" ? "essential.ready_glacier" : "essential.ready_now") : "—";
  };
  function toggle(target: Target) { setSelected((current) => current.some((row) => row.vaultId === target.vaultId && row.item.path === target.item.path) ? current.filter((row) => row.vaultId !== target.vaultId || row.item.path !== target.item.path) : [...current, target]); }
  const checked = (target: Target) => selected.some((row) => row.vaultId === target.vaultId && row.item.path === target.item.path);
  return <section className="grid min-w-0 gap-4" data-testid="file-browser">
    <div className="flex items-center justify-between gap-3"><h1 className="min-w-0 break-words text-2xl font-bold">{vaultName}</h1><Button variant="secondary" aria-expanded={settings} onClick={() => setSettings(!settings)}>{t("essential.settings")}</Button></div>
    {problemCount > 0 && <p role="status" className="rounded border border-line p-3"><a className="underline" href="/problems">{t("essential.folder_problems", { count: problemCount })}</a></p>}
    {query.data?.protection_suspended && <p role="status" className="rounded border border-line p-3">{t("state.protection_suspended")} <button className="min-h-11 underline" onClick={() => setSettings(true)}>{t("essential.review_protection")}</button></p>}
    {settings && <VaultSettingsPanel key={vaultId} vaultId={vaultId} vaultName={vaultName} onStopped={() => window.location.assign("/general")} />}
    <label className="block"><span className="sr-only">{t("essential.search")}</span><input type="search" className="min-h-11 w-full min-w-0 rounded border border-input bg-surface px-3" data-testid="file-search" placeholder={t("essential.search")} value={input} onChange={(event) => setInput(event.target.value)} /></label>
    <label className="flex flex-wrap items-center gap-2">{t("ui.filter_by_state")}<select className="min-h-11 rounded border border-input bg-surface px-3" data-testid="state-filter" value={state} onChange={(event) => update({ state: event.target.value, page: "", file: "" })}>{[["", "essential.all"], ["cloud_only", "state.cloud_only"], ["pending", "state.pending"], ["problem", "state.problem"]].map(([value, key]) => <option key={value} value={value}>{t(key)}</option>)}</select></label>
    {!global && <nav aria-label={t("ui.breadcrumb_archive")} data-testid="breadcrumbs" className="flex min-w-0 flex-wrap items-center gap-2"><button className="min-h-11 font-bold underline" data-directory="" onClick={() => openFolder("")}>{t("ui.breadcrumb_archive")}</button>{directory.split("/").filter(Boolean).map((part, index, parts) => <button key={index} data-directory={parts.slice(0, index + 1).join("/")} className="min-h-11 break-all underline" onClick={() => openFolder(parts.slice(0, index + 1).join("/"))}>/ {part}</button>)}</nav>}
    {global && <p>{t("essential.global_results")}</p>}
    {query.isPending && <p role="status" data-testid="file-list-loading">{t("ui.file_list_loading")}</p>}{query.error && <div role="alert" data-testid="file-list-error"><p>{t("ui.file_list_error")}</p><Button variant="secondary" onClick={() => void query.refetch()}>{t("ui.file_list_retry")}</Button></div>}
    {query.data?.aggregate_status === "loading" && <p role="status">{t("ui.file_list_loading")}</p>}
    <div className="flex flex-wrap gap-2 rounded border border-line p-2" aria-label={t("essential.selection")}>
      <Button disabled={!selected.length || busy || selected.some((row) => !eligible(row, "recover"))} onClick={() => setRecover([...selected])}>{t("ui.row_action_recover")}</Button>
      <Button variant="secondary" disabled={!selected.length || busy || selected.some((row) => !eligible(row, "free-space"))} onClick={() => setRelease([...selected])}>{t("essential.free_now")}</Button>
      <Button variant="secondary" disabled={selected.length !== 1 || selected[0]?.item.type !== "file"} onClick={() => setDetails(selected[0] ?? null)}>{t("essential.details")}</Button>
    </div>
    <div className="hidden min-w-0 md:block"><table className="w-full table-fixed text-sm" data-testid="file-list-table"><thead><tr><th className="w-12"><span className="sr-only">{t("essential.selection")}</span></th>{columns.map((key) => <th className="px-2 py-3 text-left" key={key}>{t(key)}</th>)}</tr></thead><tbody>{rows.map((target) => <tr key={`${target.vaultId}:${target.item.path}`} data-highlighted={target.item.path === focusPath || undefined} className={target.item.path === focusPath ? "bg-green-soft" : "border-t border-line"}><td><input className="size-5" type="checkbox" aria-label={`${t("essential.select")} ${target.vaultName} / ${target.item.path}`} checked={checked(target)} onChange={() => toggle(target)} /></td>{columns.map((key, index) => <td className="break-words px-2 py-3" key={key}>{cell(target, index)}</td>)}</tr>)}</tbody></table></div>
    <ul className="grid gap-3 md:hidden" data-testid="file-list-cards">{rows.map((target) => <li key={`${target.vaultId}:${target.item.path}`} data-highlighted={target.item.path === focusPath || undefined} className={`min-w-0 rounded-panel border border-line p-3 ${target.item.path === focusPath ? "bg-green-soft" : "bg-surface"}`}><label className="flex min-h-11 items-center gap-3"><input className="size-5" type="checkbox" aria-label={`${t("essential.select")} ${target.vaultName} / ${target.item.path}`} checked={checked(target)} onChange={() => toggle(target)} /><span className="text-sm">{t("essential.select")}</span></label>{cell(target, 0)}<dl className="grid gap-2">{columns.slice(1).map((key, index) => <div key={key}><dt className="text-xs text-muted">{t(key)}</dt><dd>{cell(target, index + 1)}</dd></div>)}</dl></li>)}</ul>
    {query.data?.items.length === 0 && query.data.aggregate_status !== "loading" && <p data-testid="file-list-empty">{t("ui.empty_no_matches")}</p>}
    <div className="flex flex-wrap items-center justify-between gap-2"><p data-testid="page-label">{query.data?.total ?? "—"} · {page}</p><div className="flex gap-2"><Button variant="secondary" disabled={page === 1 || query.isFetching} onClick={() => update({ page: String(page - 1), file: "" })}>{t("ui.previous")}</Button><Button variant="secondary" disabled={query.isFetching || page * 100 >= (query.data?.total ?? 0)} onClick={() => update({ page: String(page + 1), file: "" })}>{t("ui.next")}</Button></div></div>
    <ActiveOperations vaultId={vaultId} t={t} />
    {message && <p role="alert">{message}</p>}
    {effectiveDetails && <FileDetails key={`${effectiveDetails.vaultId}:${effectiveDetails.item.path}`} target={effectiveDetails} onClose={() => { setDetails(null); if (focusPath) update({ file: "" }); }} onRecover={() => { setRecover([effectiveDetails]); setDetails(null); if (focusPath) update({ file: "" }); }} onRefresh={refresh} t={t} />}
    {recover && <RecoverSelection targets={recover} onClose={() => setRecover(null)} onDone={() => { setRecover(null); refresh(); }} t={t} />}
    <ConfirmDialog open={Boolean(release)} onOpenChange={(open) => { if (!open && !busy) setRelease(null); }} title={t("ui.confirm_free_space_title")} description={t("ui.confirm_free_space_description", { path: release?.map((row) => `${row.vaultName} / ${row.item.path}`).join(", ") ?? "" })} confirmLabel={busy ? t("ui.loading") : t("essential.free_now")} cancelLabel={t("ui.cancel")} tone="danger" onConfirm={() => void free()} />
  </section>;
}

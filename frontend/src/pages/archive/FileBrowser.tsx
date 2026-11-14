import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, CircleAlert, CirclePause, File as FileIcon, Folder, Search, X } from "lucide-react";
import { apiRequest } from "@/api/client";
import type { ArchiveListItem, FilesResponse } from "@/api/types";
import { vaultApi } from "@/api/vault";
import { Badge, type BadgeState } from "@/components/Badge";
import { itemStateBadge } from "./protectionLabel";
import { Button } from "@/components/ui/button";
import { ActiveOperations } from "@/components/ActiveOperations";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { VaultHeader } from "@/layout/VaultHeader";
import { useVaultStats } from "@/api/useVaultStats";
import { cn } from "@/lib/utils";
import { navigate, vaultFilesUrl, vaultSettingsUrl } from "@/router";
import { FileDetails } from "./FileDetails";
import { RecoverSelection, type Target } from "./RecoverSelection";
import { formatBytes, formatDateTime, formatNumber } from "./format";
import { errorMessage } from "@/lib/errorMessage";

type Translate = (key: string, params?: Record<string, string | number>) => string;
export type FileBrowserProps = { t: Translate; vaultId: number; vaultName: string; capabilities: { delete_enabled: boolean; cloud_deletion_enabled: boolean }; problemCount?: number; onRefresh?: () => void };
export const SEARCH_DEBOUNCE_MS = 250;
const PAGE_SIZE = 100;
/** List filters; "" is every file. Counts come from the Vault-wide `/api/stats`. */
const FILTERS = [["", "files.chip_all"], ["cloud_only", "files.chip_cloud_only"], ["pending", "files.chip_pending"], ["stopped", "files.chip_stopped"], ["excluded", "files.chip_excluded"], ["unsupported", "files.chip_unsupported"], ["problem", "files.chip_problem"]] as const;
const parentOf = (path: string) => path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";

function FolderStates({ item, t }: { item: ArchiveListItem; t: Translate }) {
  const counts = Object.entries((item.state_counts ?? {}) as Record<string, number>).filter(([, count]) => count > 0);
  if (!counts.length) return <span className="text-muted">—</span>;
  return <span className="flex flex-wrap gap-1">{counts.map(([state, count]) => <Badge key={state} size="sm" state={state as BadgeState} label={`${t(`state.${state}`)} ${formatNumber(count)}`} />)}</span>;
}

export function FileBrowser({ t, vaultId, vaultName, problemCount = 0, onRefresh }: FileBrowserProps) {
  const [revision, setRevision] = useState(0);
  const [params, setParams] = useState(() => new URLSearchParams(window.location.search));
  const [input, setInput] = useState(params.get("q") ?? "");
  const [selected, setSelected] = useState<Target[]>([]);
  const [details, setDetails] = useState<Target | null>(null);
  const [recover, setRecover] = useState<Target[] | null>(null);
  const [release, setRelease] = useState<Target[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [freeOutcome, setFreeOutcome] = useState<{ freed: number; total: number; errors: string[] } | null>(null);
  const scope = useMemo(() => vaultApi(vaultId), [vaultId]);
  const stats = useVaultStats(vaultId);
  const q = params.get("q") ?? "", state = params.get("state") ?? "", directory = params.get("directory") ?? "";
  const allVaults = params.get("scope") === "all";
  const page = Math.max(1, Number(params.get("page")) || 1);
  const global = Boolean(q) && allVaults;
  function update(values: Record<string, string>, push = false) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(values)) { if (value) next.set(key, value); else next.delete(key); }
    window.history[push ? "pushState" : "replaceState"]({}, "", `${window.location.pathname}?${next}`);
    setParams(next); setSelected([]); setDetails(null);
  }
  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (input === q) return;
      const next = new URLSearchParams(window.location.search);
      if (input) next.set("q", input); else next.delete("q");
      next.delete("page"); next.delete("file");
      window.history.replaceState({}, "", `${window.location.pathname}?${next}`); setParams(next); setSelected([]); setDetails(null);
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [input, q]);
  useEffect(() => {
    const pop = () => { const next = new URLSearchParams(window.location.search); setParams(next); setInput(next.get("q") ?? ""); setSelected([]); setDetails(null); };
    window.addEventListener("popstate", pop); return () => window.removeEventListener("popstate", pop);
  }, []);
  const query = useQuery({ queryKey: ["files", vaultId, q, state, directory, page, global, revision], queryFn: () => {
    const search = new URLSearchParams({ q, state, directory: q ? "" : directory, page: String(page), page_size: String(PAGE_SIZE) });
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
  /** Frees every target; a failure does not stop the rest and is reported as «X of N freed» (#652). */
  async function free() {
    if (!release || busy) return;
    setBusy(true); setMessage(""); setFreeOutcome(null);
    const errors: string[] = [];
    for (const target of release) {
      try { await vaultApi(target.vaultId).action("free-space", { path: target.item.path, is_directory: target.item.type === "directory" }); }
      catch (error) { errors.push(`${target.vaultName} / ${target.item.path}: ${errorMessage(error, t)}`); }
    }
    if (errors.length) setFreeOutcome({ freed: release.length - errors.length, total: release.length, errors });
    setRelease(null); refresh(); setBusy(false);
  }
  /** Stopped Uploads of this level (#630): files, and folders holding some. */
  const stoppedTargets = global ? [] : rows.filter(({ item }) => item.type === "directory" ? Number((item.state_counts as Record<string, number> | undefined)?.stopped ?? 0) > 0 : item.state === "stopped");
  const stoppedCount = stoppedTargets.reduce((sum, { item }) => sum + (item.type === "directory" ? Number((item.state_counts as Record<string, number>).stopped) : 1), 0);
  async function uploadStopped() {
    if (busy) return;
    setBusy(true); setMessage("");
    try { for (const target of stoppedTargets) await vaultApi(target.vaultId).post("/api/upload", { path: target.item.path, is_directory: target.item.type === "directory" }); refresh(); }
    catch (error) { setMessage(errorMessage(error, t)); }
    finally { setBusy(false); }
  }
  const counts = (stats.data?.states ?? {}) as Record<string, number>;
  const count = (value: string) => value ? counts[value] ?? 0 : Object.values(counts).reduce((sum, n) => sum + n, 0);
  const total = query.data?.total ?? 0;
  const from = total ? (page - 1) * PAGE_SIZE + 1 : 0, to = Math.min(page * PAGE_SIZE, total);
  const searching = Boolean(q);
  const label = (target: Target) => global ? `${target.vaultName} / ${target.item.path}` : searching ? target.item.path : target.item.name;
  function open(target: Target) {
    const item = target.item;
    if (item.type === "directory") openFolder(item.path);
    else if (global) navigate(vaultFilesUrl(target.vaultId, { directory: parentOf(item.path), file: item.path }));
    else setDetails(target);
  }
  const name = (target: Target) => <button type="button" className="flex min-h-11 max-w-full min-w-0 items-center gap-2 text-left font-semibold hover:underline focus-visible:underline" onClick={() => open(target)}>
    {target.item.type === "directory" ? <Folder className="size-4 shrink-0 text-green" aria-hidden="true" /> : <FileIcon className="size-4 shrink-0 text-muted" aria-hidden="true" />}
    <span className="min-w-0 break-words">{label(target)}</span>
  </button>;
  const protection = (item: ArchiveListItem) => item.type === "directory" ? <FolderStates item={item} t={t} /> : item.recovery?.active
    ? <span className="flex flex-wrap gap-1"><Badge size="sm" {...itemStateBadge(item, t)} /><Badge size="sm" state="restoring" label={t("state.recovering")} /></span>
    : <Badge size="sm" {...itemStateBadge(item, t)} />;
  const size = (item: ArchiveListItem) => formatBytes(item.type === "directory" ? item.total_size : Number((item.local_exists ? item.local_size : item.cloud_size) ?? 0));
  const modified = (item: ArchiveListItem) => formatDateTime(item.modified_at);
  function toggle(target: Target) { setSelected((current) => current.some((row) => row.vaultId === target.vaultId && row.item.path === target.item.path) ? current.filter((row) => row.vaultId !== target.vaultId || row.item.path !== target.item.path) : [...current, target]); }
  const checked = (target: Target) => selected.some((row) => row.vaultId === target.vaultId && row.item.path === target.item.path);
  const allChecked = rows.length > 0 && rows.every(checked);
  const checkbox = (target: Target) => <input className="size-5 accent-[var(--green)]" type="checkbox" aria-label={`${t("essential.select")} ${target.vaultName} / ${target.item.path}`} checked={checked(target)} onChange={() => toggle(target)} />;
  const highlighted = (target: Target) => target.item.path === focusPath;
  const attention = problemCount > 0;
  return <section className="grid min-w-0 gap-4" data-testid="file-browser">
    <VaultHeader vaultId={vaultId} vaultName={vaultName} active="files" t={t} />
    {attention && <div role="status" className="flex min-w-0 items-start gap-3 rounded-card border border-[var(--state-problem-bg)] bg-red-soft/60 p-3 text-sm" data-testid="vault-attention">
      <CircleAlert className="mt-0.5 size-4 shrink-0 text-destructive" aria-hidden="true" />
      <p className="flex min-w-0 flex-wrap gap-x-3 gap-y-1">
        {problemCount > 0 && <a className="font-semibold underline" href="/problems">{t("files.open_problems", { count: problemCount })}</a>}
      </p>
    </div>}
    {query.data?.manual_upload && <p className="text-sm text-muted" data-testid="vault-manual-upload">{t("files.manual_upload_note")} <a className="font-semibold text-ink underline" href={vaultSettingsUrl(vaultId)}>{t("essential.review_protection")}</a></p>}
    <ActiveOperations vaultId={vaultId} t={t} />
    <div className="grid min-w-0 gap-3">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <label className="relative min-w-0 flex-1 basis-64"><span className="sr-only">{t(allVaults ? "files.search_all" : "files.search_vault")}</span>
          <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted" aria-hidden="true" />
          <input type="search" className="min-h-11 w-full min-w-0 rounded-lg border border-input bg-surface pr-3 pl-9" data-testid="file-search" placeholder={t(allVaults ? "files.search_all" : "files.search_vault")} value={input} onChange={(event) => setInput(event.target.value)} /></label>
        <div role="group" aria-label={t("files.scope_label")} className="inline-flex shrink-0 rounded-lg border border-input bg-surface p-0.5" data-testid="search-scope">
          {([["", "files.scope_vault"], ["all", "files.scope_all"]] as const).map(([value, key]) => <button key={key} type="button" aria-pressed={allVaults === (value === "all")}
            className="min-h-10 rounded-md px-3 text-sm font-semibold text-muted aria-pressed:bg-green-soft aria-pressed:text-ink" onClick={() => update({ scope: value, page: "", file: "" })}>{t(key)}</button>)}
        </div>
      </div>
      <div role="group" aria-label={t("ui.filter_by_state")} className="flex min-w-0 flex-wrap gap-2" data-testid="state-filter">
        {FILTERS.map(([value, key]) => <button key={key} type="button" aria-pressed={state === value} data-filter={value || "all"}
          className="inline-flex min-h-11 items-center gap-2 rounded-badge border border-input bg-surface px-3.5 text-sm font-semibold hover:border-[var(--interactive-border-hover)] aria-pressed:border-green aria-pressed:bg-green-soft"
          onClick={() => update({ state: value, page: "", file: "" })}>{t(key)}{!global && stats.data && <>{" "}<span className="tabular-nums text-muted">{formatNumber(count(value))}</span></>}</button>)}
      </div>
    </div>
    {!searching && <nav aria-label={t("files.breadcrumb")} data-testid="breadcrumbs" className="flex min-w-0 flex-wrap items-center gap-1 text-sm">
      <button type="button" className="min-h-11 rounded px-1 font-bold hover:underline" data-directory="" onClick={() => openFolder("")}>{vaultName}</button>
      {directory.split("/").filter(Boolean).map((part, index, parts) => <span key={index} className="inline-flex min-w-0 items-center gap-1"><ChevronRight className="size-4 shrink-0 text-muted" aria-hidden="true" />
        <button type="button" data-directory={parts.slice(0, index + 1).join("/")} aria-current={index === parts.length - 1 ? "location" : undefined} className="min-h-11 rounded px-1 break-all hover:underline aria-[current=location]:font-semibold" onClick={() => openFolder(parts.slice(0, index + 1).join("/"))}>{part}</button></span>)}
    </nav>}
    {searching && <p className="text-sm text-muted">{global ? t("files.results_all") : t("files.results_vault", { name: vaultName })}</p>}
    {query.isPending && <p role="status" data-testid="file-list-loading">{t("ui.file_list_loading")}</p>}{query.error && <div role="alert" data-testid="file-list-error"><p>{t("ui.file_list_error")}</p><Button variant="secondary" onClick={() => void query.refetch()}>{t("ui.file_list_retry")}</Button></div>}
    {query.data?.aggregate_status === "loading" && <p role="status">{t("ui.file_list_loading")}</p>}
    {stoppedCount > 0 && <div role="note" className="flex min-w-0 flex-wrap items-center gap-3 rounded-card border border-line bg-surface p-3 text-sm" data-testid="stopped-uploads">
      <CirclePause className="size-4 shrink-0 text-muted" aria-hidden="true" />
      <p className="min-w-0 flex-1">{t("files.stopped_notice", { count: formatNumber(stoppedCount) })}</p>
      <Button variant="secondary" disabled={busy} onClick={() => void uploadStopped()}>{t("essential.upload_now")}</Button>
    </div>}
    {selected.length > 0 && <div className="sticky top-[72px] z-10 flex min-w-0 flex-wrap items-center gap-2 rounded-card border border-green bg-surface p-2 shadow-md" role="region" aria-label={t("files.selection_bar")} data-testid="selection-bar">
      <span className="px-2 font-semibold tabular-nums" role="status">{t("files.selected", { count: selected.length })}</span>
      <Button disabled={busy || selected.some((row) => !eligible(row, "recover"))} onClick={() => setRecover([...selected])}>{t("ui.row_action_recover")}</Button>
      <Button variant="secondary" disabled={busy || selected.some((row) => !eligible(row, "free-space"))} onClick={() => setRelease([...selected])}>{t("essential.free_now")}</Button>
      {selected.length === 1 && selected[0]?.item.type === "file" && <Button variant="secondary" onClick={() => setDetails(selected[0] ?? null)}>{t("essential.details")}</Button>}
      <Button variant="ghost" className="ml-auto" onClick={() => setSelected([])}><X aria-hidden="true" />{t("files.clear_selection")}</Button>
    </div>}
    {freeOutcome && <div role="alert" className="flex min-w-0 items-start gap-3 rounded-card border border-[var(--state-problem-bg)] bg-red-soft/60 p-3 text-sm" data-testid="free-space-outcome">
      <CircleAlert className="mt-0.5 size-4 shrink-0 text-destructive" aria-hidden="true" />
      <div className="grid min-w-0 flex-1 gap-1">
        <p className="font-semibold">{t("files.free_space_partial", { freed: formatNumber(freeOutcome.freed), total: formatNumber(freeOutcome.total) })}</p>
        <ul className="grid gap-1">{freeOutcome.errors.map((error, index) => <li key={index} className="break-words">{error}</li>)}</ul>
      </div>
      <Button variant="ghost" className="shrink-0" aria-label={t("ui.close")} onClick={() => setFreeOutcome(null)}><X aria-hidden="true" /></Button>
    </div>}
    <div className="hidden min-w-0 overflow-hidden rounded-card border border-line bg-surface md:block">
      <table className="w-full table-fixed text-sm" data-testid="file-list-table">
        <thead className="bg-canvas/60 text-left text-xs font-semibold text-muted uppercase"><tr>
          <th className="w-12 px-3 py-2"><input className="size-5 accent-[var(--green)]" type="checkbox" aria-label={t("files.select_page")} checked={allChecked} disabled={!rows.length} onChange={() => setSelected(allChecked ? [] : rows)} /></th>
          <th className="px-2 py-2">{t("essential.name")}</th><th className="w-[34%] px-2 py-2">{t("essential.protection")}</th>
          <th className="w-28 px-2 py-2 text-right">{t("essential.size")}</th><th className="w-44 px-2 py-2">{t("essential.modified")}</th>
        </tr></thead>
        <tbody>{rows.map((target) => <tr key={`${target.vaultId}:${target.item.path}`} data-highlighted={highlighted(target) || undefined}
          className={cn("border-t border-line", highlighted(target) ? "bg-green-soft" : checked(target) ? "bg-green-soft/50" : "hover:bg-canvas/60")}>
          <td className="px-3">{checkbox(target)}</td>
          <td className="min-w-0 px-2 py-1">{name(target)}</td>
          <td className="px-2 py-2">{protection(target.item)}</td>
          <td className="px-2 py-2 text-right tabular-nums">{size(target.item)}</td>
          <td className="px-2 py-2 text-muted">{modified(target.item)}</td>
        </tr>)}</tbody>
      </table>
    </div>
    <ul className="grid min-w-0 gap-2 md:hidden" data-testid="file-list-cards">{rows.map((target) => <li key={`${target.vaultId}:${target.item.path}`} data-highlighted={highlighted(target) || undefined}
      className={cn("flex min-w-0 items-start gap-3 rounded-card border border-line p-3", highlighted(target) || checked(target) ? "bg-green-soft" : "bg-surface")}>
      <span className="flex min-h-11 items-center">{checkbox(target)}</span>
      <div className="grid min-w-0 flex-1 gap-1">
        {name(target)}
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-sm">{protection(target.item)}<span className="tabular-nums text-muted">{size(target.item)}</span></div>
        <span className="text-xs text-muted">{t("essential.modified")}: {modified(target.item)}</span>
      </div>
    </li>)}</ul>
    {query.data?.items.length === 0 && query.data.aggregate_status !== "loading" && <p className="rounded-card border border-dashed border-line p-6 text-center text-muted" data-testid="file-list-empty">{t("ui.empty_no_matches")}</p>}
    {total > 0 && <nav aria-label={t("files.pages")} className="flex flex-wrap items-center justify-between gap-2">
      <p data-testid="page-label" className="text-sm text-muted tabular-nums">{t("files.range", { from: formatNumber(from), to: formatNumber(to), total: formatNumber(total) })}</p>
      {total > PAGE_SIZE && <div className="flex gap-2">
        <Button variant="secondary" disabled={page === 1 || query.isFetching} onClick={() => update({ page: String(page - 1), file: "" })}><ChevronLeft aria-hidden="true" />{t("ui.previous")}</Button>
        <Button variant="secondary" disabled={query.isFetching || page * PAGE_SIZE >= total} onClick={() => update({ page: String(page + 1), file: "" })}>{t("ui.next")}<ChevronRight aria-hidden="true" /></Button>
      </div>}
    </nav>}
    {message && <p role="alert">{message}</p>}
    {effectiveDetails && <FileDetails key={`${effectiveDetails.vaultId}:${effectiveDetails.item.path}`} target={effectiveDetails} onClose={() => { setDetails(null); if (focusPath) update({ file: "" }); }} onRecover={() => { setRecover([effectiveDetails]); setDetails(null); if (focusPath) update({ file: "" }); }} onRefresh={refresh} t={t} />}
    {recover && <RecoverSelection targets={recover} onClose={() => setRecover(null)} onDone={() => { setRecover(null); refresh(); }} t={t} />}
    <ConfirmDialog open={Boolean(release)} onOpenChange={(open) => { if (!open && !busy) setRelease(null); }} title={t("ui.confirm_free_space_title")} description={t("ui.confirm_free_space_description", { count: formatNumber(release?.length ?? 0) })} confirmLabel={busy ? t("ui.loading") : t("essential.free_now")} cancelLabel={t("ui.cancel")} tone="danger" keepOpenOnConfirm onConfirm={() => void free()}>
      <details className="rounded-card border border-line" data-testid="free-space-items"><summary className="min-h-11 cursor-pointer px-3 py-3 text-sm font-semibold">{t("files.free_space_show_items", { count: formatNumber(release?.length ?? 0) })}</summary>
        <ul className="grid gap-1 border-t border-line p-3 text-sm">{release?.map((row) => <li key={`${row.vaultId}:${row.item.path}`} className="break-all">{`${row.vaultName} / ${row.item.path}`}</li>)}</ul></details>
    </ConfirmDialog>
  </section>;
}

import { useMemo, useState, type ReactNode } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Archive, CircleAlert, Clock, Euro, Plus, ShieldCheck } from "lucide-react";

import { fetchGeneralVaults, type GeneralVault, type StatsResponse, type VaultListItem } from "@/api";
import { vaultApi, type Overview } from "@/api/vault";
import { VaultStateBadge } from "@/components/VaultStateBadge";
import { vaultState } from "@/lib/vaultState";
import { StopProtectionDialog } from "@/components/StopProtectionDialog";
import { buttonVariants } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { formatBytes } from "@/pages/archive/format";
import { vaultFilesUrl } from "@/router";

const STATS_REFRESH_MS = 60_000;
type SortKey = "name" | "state" | "protected" | "local" | "cloud" | "cost" | "problems";
/** Worst first, so an ascending sort puts the Vaults that need attention on top. */
const STATE_RANK: Record<string, number> = { suspended: 0, problem: 1, unconfirmed: 2, pending: 3, cloud_only: 4, protected: 5, excluded: 6, empty: 7, unknown: 8 };

type Row = {
  id: number; name: string; state: string; protected: number; files: number; local: number; cloud: number;
  cost: number | null; problems: number; loaded: boolean;
};

function rowFor(vault: VaultListItem, stats: StatsResponse | undefined, overview: Overview | undefined): Row {
  const states = (stats?.states ?? {}) as Record<string, number>;
  const storage = (stats?.storage ?? {}) as { local_bytes?: number; cloud_bytes?: number };
  const summary = overview?.vaults.find((item) => item.id === vault.id);
  const state = vaultState(stats);
  return {
    id: vault.id, name: vault.name, state, loaded: Boolean(stats),
    protected: states.protected ?? 0,
    files: Object.entries(states).filter(([name]) => name !== "excluded").reduce((sum, [, count]) => sum + count, 0),
    local: storage.local_bytes ?? 0, cloud: storage.cloud_bytes ?? 0,
    cost: summary ? summary.monthly_estimate : null, problems: summary?.open_problem_count ?? 0,
  };
}

function Tile({ icon, label, value, detail, tone = "neutral", testId }: {
  icon: ReactNode; label: string; value: ReactNode; detail?: ReactNode; tone?: "neutral" | "good" | "warn" | "bad"; testId?: string;
}) {
  const accent = { neutral: "text-muted", good: "text-green", warn: "text-[var(--state-restoring-fg)]", bad: "text-destructive" }[tone];
  return <div className="grid min-w-0 content-start gap-1 rounded-card border border-line bg-surface p-4" data-testid={testId}>
    <p className="flex items-center gap-2 text-sm font-semibold text-muted"><span className={accent}>{icon}</span>{label}</p>
    <p className="text-2xl font-extrabold tabular-nums">{value}</p>
    {detail ? <div className="text-sm text-muted">{detail}</div> : null}
  </div>;
}

/** Home: how protected is everything, and which Vault needs attention. */
export function OverviewPage({ vaults, overview }: { vaults: VaultListItem[]; overview?: Overview }) {
  const { t, locale } = useI18n();
  const [sort, setSort] = useState<{ key: SortKey; ascending: boolean }>({ key: "state", ascending: true });
  const [stopped, setStopped] = useState<GeneralVault | null>(null);
  const general = useQuery({ queryKey: ["general-vaults"], queryFn: fetchGeneralVaults });
  const stats = useQueries({ queries: vaults.map((vault) => ({
    queryKey: ["vault-stats", vault.id],
    queryFn: () => vaultApi(vault.id).request<StatsResponse>("/api/stats"),
    refetchInterval: STATS_REFRESH_MS,
  })) });
  const number = useMemo(() => new Intl.NumberFormat(locale), [locale]);
  const money = (value: number) => `${value.toFixed(2)} ${overview?.currency ?? ""}`.trim();
  const rows = vaults.map((vault, index) => rowFor(vault, stats[index]?.data, overview));
  const sorted = [...rows].sort((a, b) => {
    const value = (row: Row) => sort.key === "name" ? row.name.toLocaleLowerCase() : sort.key === "state" ? STATE_RANK[row.state] ?? 9 : row[sort.key] ?? -1;
    const [left, right] = [value(a), value(b)];
    const order = left < right ? -1 : left > right ? 1 : a.name.localeCompare(b.name);
    return sort.ascending ? order : -order;
  });
  const totals = rows.reduce((sum, row) => ({ protected: sum.protected + row.protected, files: sum.files + row.files }), { protected: 0, files: 0 });
  const pending = stats.reduce((sum, query) => sum + Number((query.data?.states as Record<string, number> | undefined)?.pending ?? 0), 0);
  const loaded = stats.length > 0 && stats.every((query) => query.data || query.error);
  const archived = (general.data?.items ?? []).filter((vault) => vault.decommission_state && vault.decommission_state !== "active");
  const problems = overview?.open_problem_count;
  const columns: { key: SortKey; label: string; numeric?: boolean }[] = [
    { key: "name", label: t("overview.col_vault") }, { key: "state", label: t("overview.col_state") },
    { key: "protected", label: t("overview.col_files"), numeric: true }, { key: "local", label: t("overview.col_local"), numeric: true },
    { key: "cloud", label: t("overview.col_cloud"), numeric: true }, { key: "cost", label: t("overview.col_cost"), numeric: true },
    { key: "problems", label: t("overview.col_problems"), numeric: true },
  ];
  const cell = (row: Row, key: SortKey): ReactNode => {
    if (key === "name") return <a href={vaultFilesUrl(row.id)} className="font-bold text-ink no-underline hover:underline">{row.name}</a>;
    if (key === "state") return <VaultStateBadge state={row.state} t={t} />;
    if (!row.loaded && key !== "cost" && key !== "problems") return <span className="text-muted">—</span>;
    if (key === "protected") return `${number.format(row.protected)} / ${number.format(row.files)}`;
    if (key === "local") return formatBytes(row.local);
    if (key === "cloud") return formatBytes(row.cloud);
    if (key === "cost") return row.cost === null ? "—" : money(row.cost);
    return row.problems ? <span className="font-bold text-destructive">{number.format(row.problems)}</span> : <span className="text-muted">0</span>;
  };

  return <div className="grid min-w-0 gap-6" data-testid="overview">
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0"><h1 className="text-2xl font-bold">{t("nav.overview")}</h1><p className="text-sm text-muted">{t("overview.subtitle")}</p></div>
      <a href="/vaults/new" className={buttonVariants({ variant: "primary", className: "no-underline" })}><Plus aria-hidden="true" />{t("nav.new_vault")}</a>
    </div>

    {vaults.length === 0 ? <section className="grid justify-items-start gap-3 rounded-panel border border-dashed border-line bg-surface p-6">
      <h2 className="text-lg font-bold">{t("overview.no_vaults_title")}</h2><p className="text-muted">{t("overview.no_vaults_body")}</p>
      <a href="/vaults/new" className={buttonVariants({ variant: "primary", className: "no-underline" })}><Plus aria-hidden="true" />{t("nav.new_vault")}</a>
    </section> : <>
      <section className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label={t("overview.summary")}>
        <Tile testId="tile-protected" icon={<ShieldCheck className="size-4" />} tone="good" label={t("overview.tile_protected")}
          value={loaded ? number.format(totals.protected) : "—"} detail={loaded ? t("overview.tile_protected_of", { total: number.format(totals.files) }) : null} />
        <Tile testId="tile-pending" icon={<Clock className="size-4" />} tone={pending ? "warn" : "neutral"} label={t("overview.tile_pending")}
          value={loaded ? number.format(pending) : "—"} detail={t("overview.tile_pending_hint")} />
        <Tile testId="tile-problems" icon={<CircleAlert className="size-4" />} tone={problems ? "bad" : "neutral"} label={t("overview.tile_problems")}
          value={problems === undefined ? "—" : number.format(problems)}
          detail={problems ? <a href="/problems" className="font-semibold text-ink underline">{t("overview.tile_problems_link")}</a> : t("overview.tile_problems_none")} />
        <Tile testId="tile-cost" icon={<Euro className="size-4" />} label={t("overview.tile_cost", { month: overview?.month ?? "—" })}
          value={overview ? money(overview.monthly_estimate) : "—"}
          detail={overview ? <span title={t("essential.estimate_caveat")}>{overview.previous_month_delta === null ? t("essential.no_comparison")
            : t("essential.month_delta", { cost: overview.previous_month_delta.toFixed(2), currency: overview.currency })}</span> : null} />
      </section>

      <section className="grid min-w-0 gap-2" aria-labelledby="overview-vaults">
        <h2 id="overview-vaults" className="text-lg font-bold">{t("nav.vaults")}</h2>
        <div className="hidden overflow-hidden rounded-card border border-line bg-surface md:block">
          <table className="w-full text-sm" data-testid="overview-table">
            <thead className="border-b border-line bg-canvas/60 text-left text-xs text-muted uppercase"><tr>{columns.map((column) => {
              const active = sort.key === column.key;
              return <th key={column.key} scope="col" aria-sort={active ? (sort.ascending ? "ascending" : "descending") : "none"} className={cn("px-3 py-2 font-bold", column.numeric && "text-right")}>
                <button type="button" className={cn("inline-flex min-h-9 items-center gap-1 uppercase", column.numeric && "flex-row-reverse")} aria-label={t("overview.sort_by", { column: column.label })}
                  onClick={() => setSort({ key: column.key, ascending: active ? !sort.ascending : true })}>
                  {column.label}{active ? (sort.ascending ? <ArrowUp className="size-3" aria-hidden="true" /> : <ArrowDown className="size-3" aria-hidden="true" />) : null}
                </button></th>;
            })}</tr></thead>
            <tbody>{sorted.map((row) => <tr key={row.id} className="border-t border-line first:border-t-0 hover:bg-green-soft/40" data-testid="overview-row">
              {columns.map((column) => <td key={column.key} className={cn("px-3 py-2.5", column.numeric && "text-right tabular-nums")}>{cell(row, column.key)}</td>)}
            </tr>)}</tbody>
          </table>
        </div>
        <ul className="grid gap-2 md:hidden" data-testid="overview-cards">{sorted.map((row) => <li key={row.id}>
          <a href={vaultFilesUrl(row.id)} className="grid min-w-0 gap-2 rounded-card border border-line bg-surface p-3 text-ink no-underline">
            <span className="flex min-w-0 items-start justify-between gap-2"><span className="min-w-0 font-bold break-words">{row.name}</span><VaultStateBadge state={row.state} t={t} /></span>
            <span className="grid grid-cols-2 gap-x-3 gap-y-1 text-sm">
              <span className="text-muted">{t("overview.col_files")}</span><span className="text-right tabular-nums">{cell(row, "protected")}</span>
              <span className="text-muted">{t("overview.col_local")}</span><span className="text-right tabular-nums">{cell(row, "local")}</span>
              <span className="text-muted">{t("overview.col_cost")}</span><span className="text-right tabular-nums">{cell(row, "cost")}</span>
              {row.problems ? <><span className="text-muted">{t("overview.col_problems")}</span><span className="text-right">{cell(row, "problems")}</span></> : null}
            </span>
          </a></li>)}</ul>
        <p className="text-xs text-muted">{t("essential.estimate_caveat")}</p>
      </section>
    </>}

    {archived.length > 0 && <details className="rounded-card border border-line bg-surface p-3" data-testid="overview-archived">
      <summary className="flex min-h-11 cursor-pointer items-center gap-2 font-semibold"><Archive className="size-4 text-muted" aria-hidden="true" />{t("overview.archived", { count: archived.length })}</summary>
      <p className="mb-2 text-sm text-muted">{t("overview.archived_help")}</p>
      <ul className="grid">{archived.map((vault) => <li key={vault.id} className="flex min-w-0 flex-wrap items-center justify-between gap-2 border-t border-line py-2">
        <span className="min-w-0 break-words">{vault.name}</span>
        <button type="button" className={buttonVariants({ variant: "secondary", size: "sm" })} onClick={() => setStopped(vault)}>{t("stop_protection.status")}</button>
      </li>)}</ul>
    </details>}
    <StopProtectionDialog open={stopped !== null} onOpenChange={(open) => { if (!open) setStopped(null); }} vaultId={stopped?.id ?? 0} vaultName={stopped?.name ?? ""} onCompleted={async () => { await general.refetch(); }} />
  </div>;
}

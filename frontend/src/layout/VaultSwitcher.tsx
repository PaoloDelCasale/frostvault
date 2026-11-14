import { useState } from "react";
import { Check, ChevronsUpDown, Plus, Vault } from "lucide-react";

import type { VaultListItem } from "@/api/types";
import { Popover } from "@/components/Popover";
import { ProblemCount } from "@/components/ProblemCount";
import { cn } from "@/lib/utils";
import { vaultFilesUrl } from "@/router";
import { shellLabel, type ShellTranslator } from "./labels";

/** Lists beyond this many Vaults get a filter field. */
export const SWITCHER_FILTER_THRESHOLD = 8;

export function VaultSwitcher({ vaults, currentVaultId, allVaults = false, problemCounts = {}, t, onNavigate }: {
  vaults: VaultListItem[]; currentVaultId?: number; allVaults?: boolean; problemCounts?: Record<number, number>;
  t?: ShellTranslator; onNavigate: (url: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const current = vaults.find((vault) => vault.id === currentVaultId);
  const label = current?.name ?? (allVaults ? shellLabel(t, "nav.all_vaults", "All Vaults") : shellLabel(t, "nav.no_vault_selected", "No Vault"));
  const needle = filter.trim().toLocaleLowerCase();
  const shown = needle ? vaults.filter((vault) => vault.name.toLocaleLowerCase().includes(needle)) : vaults;
  const go = (url: string) => { setOpen(false); setFilter(""); onNavigate(url); };
  const item = "flex min-h-11 w-full min-w-0 items-center gap-2 rounded-lg px-2.5 text-left text-sm hover:bg-green-soft focus-visible:bg-green-soft focus-visible:outline-none";
  return <Popover open={open} onOpenChange={(next) => { setOpen(next); if (!next) setFilter(""); }} label={shellLabel(t, "nav.switch_vault", "Switch Vault")}
    trigger={<button type="button" data-testid="vault-switcher" aria-label={`${shellLabel(t, "nav.switch_vault", "Switch Vault")}: ${label}`}
      className="flex min-h-11 min-w-0 max-w-full items-center gap-2 rounded-lg border border-input bg-surface px-3 text-sm font-bold hover:border-[var(--interactive-border-hover)] hover:bg-green-soft aria-expanded:bg-green-soft">
      <Vault className="size-4 shrink-0 text-green" aria-hidden="true" />
      <span className="min-w-0 truncate">{label}</span>
      <ChevronsUpDown className="size-4 shrink-0 text-muted" aria-hidden="true" />
    </button>}>
    {vaults.length > SWITCHER_FILTER_THRESHOLD && <input type="search" autoFocus value={filter} onChange={(event) => setFilter(event.target.value)}
      placeholder={shellLabel(t, "nav.search_vaults", "Search Vaults")} aria-label={shellLabel(t, "nav.search_vaults", "Search Vaults")}
      className="mb-2 min-h-11 w-full rounded-lg border border-input bg-surface px-3 text-sm" />}
    <ul className="grid max-h-[min(22rem,60svh)] gap-0.5 overflow-y-auto" aria-label={shellLabel(t, "nav.vaults", "Vaults")}>
      {shown.map((vault) => <li key={vault.id}>
        <button type="button" className={cn(item, vault.id === currentVaultId && "font-bold")} aria-current={vault.id === currentVaultId ? "true" : undefined} onClick={() => go(vaultFilesUrl(vault.id))}>
          <Check className={cn("size-4 shrink-0 text-green", vault.id !== currentVaultId && "invisible")} aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate">{vault.name}</span>
          {problemCounts[vault.id] ? <ProblemCount count={problemCounts[vault.id]}>
            {shellLabel(t, "nav.problem_count", "{count} problems", { count: problemCounts[vault.id] })}</ProblemCount> : null}
        </button>
      </li>)}
      {!shown.length && <li className="px-2.5 py-3 text-sm text-muted">{shellLabel(t, "nav.no_vault_match", "No Vault matches")}</li>}
    </ul>
    <div className="mt-2 grid gap-0.5 border-t border-line pt-2">
      <button type="button" className={item} onClick={() => go("/vaults/new")}><Plus className="size-4 shrink-0" aria-hidden="true" />{shellLabel(t, "nav.new_vault", "New Vault")}</button>
    </div>
  </Popover>;
}

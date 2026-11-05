import { Button } from "@/components/ui/button";
import { shellLabel } from "./labels";
import type { ShellCapabilities, ShellNavHandlers } from "./types";

export function ShellNavItems({ capabilities, handlers, t, onNavigate, problemCounts = {}, filter = "" }: {
  capabilities: ShellCapabilities; handlers?: ShellNavHandlers; t?: (key: string) => string;
  onNavigate?: () => void; problemCounts?: Record<number, number>; filter?: string;
}) {
  const run = (fn?: () => void) => { fn?.(); onNavigate?.(); };
  return <div className="flex min-w-0 flex-col gap-2">
    <h2 className="px-2 text-xs font-bold uppercase text-muted">{shellLabel(t, "essential.protected_folders", "Protected folders")}</h2>
    {capabilities.vaults.map((vault) => <Button key={vault.id} variant="ghost" className="min-h-11 w-full justify-start whitespace-normal text-left" aria-current={capabilities.currentVaultId === vault.id ? "page" : undefined} onClick={() => run(() => handlers?.onVaultChange?.(vault.id))}>
      <span className="min-w-0 flex-1 truncate">{vault.name}</span>{problemCounts[vault.id] ? <span className="text-[var(--state-missing-fg)]" aria-label={`${problemCounts[vault.id]} ${shellLabel(t, "essential.problems", "Problems")}`}>● {problemCounts[vault.id]}</span> : null}
    </Button>)}
    <h2 className="mt-4 px-2 text-xs font-bold uppercase text-muted">{shellLabel(t, "essential.show", "Show")}</h2>
    {[["", "essential.all", "All files"], ["cloud_only", "state.cloud_only", "Cloud only"], ["pending", "state.pending", "Pending"], ["problem", "state.problem", "Problems"]].map(([value, key, fallback]) => <Button key={value} variant="ghost" className="min-h-11 justify-start" aria-pressed={filter === value} onClick={() => run(() => handlers?.onFilterChange?.(value))}>{shellLabel(t, key, fallback)}</Button>)}
    <Button variant="secondary" className="mt-4 min-h-11 justify-start" onClick={() => run(handlers?.onGeneral)}>{shellLabel(t, "essential.general", "General")}</Button>
  </div>;
}

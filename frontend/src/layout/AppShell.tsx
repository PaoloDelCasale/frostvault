import { useMemo, useState, type ReactNode } from "react";
import type { QueryClient } from "@tanstack/react-query";
import { createAppQueryClient } from "@/api";
import { AccountPreferencesMenu } from "@/components/AccountPreferencesMenu";
import { Button } from "@/components/ui/button";
import { NotificationCenter } from "@/components/NotificationCenter";
import { Dialog } from "@/components/Dialog";
import { shellLabel } from "./labels";
import { ShellNavItems } from "./ShellNavItems";
import type { ShellCapabilities, ShellNavHandlers } from "./types";

export function AppShell({ capabilities, handlers, t, queryClient: provided, children, problemCounts = {}, openProblems = 0, filter = "", spend }: {
  capabilities: ShellCapabilities; handlers?: ShellNavHandlers; t?: (key: string, params?: Record<string, unknown>) => string;
  queryClient?: QueryClient; children: ReactNode; problemCounts?: Record<number, number>; openProblems?: number; filter?: string; spend?: ReactNode;
}) {
  const [drawer, setDrawer] = useState(false);
  const fallback = useMemo(() => createAppQueryClient(), []);
  const nav = <ShellNavItems capabilities={capabilities} handlers={handlers} t={t} problemCounts={problemCounts} filter={filter} onNavigate={() => setDrawer(false)} />;
  return <div className="min-h-svh bg-canvas text-ink">
    <a href="#main-content" className="skip-link" onClick={(event) => { event.preventDefault(); document.getElementById("main-content")?.focus(); }}>{shellLabel(t, "ui.skip_to_main", "Skip to main content")}</a>
    <header className="border-b border-line bg-surface px-3 py-3">
      <div className="flex flex-nowrap items-center gap-2" data-testid="app-shell-header-row">
        <Button className="min-h-11 min-w-11 md:hidden" variant="secondary" aria-label={shellLabel(t, "ui.open_navigation", "Open navigation")} onClick={() => setDrawer(true)}>☰</Button>
        <span className="min-w-0 flex-1 truncate font-extrabold tracking-tight text-green">FrostVault</span>
        <Button variant="secondary" className="min-h-11 shrink-0" onClick={handlers?.onProblems}>{openProblems} {shellLabel(t, "essential.problems", "Problems")}</Button>
        <NotificationCenter locale={capabilities.locale} t={t} queryClient={provided ?? fallback} />
        <AccountPreferencesMenu locale={capabilities.locale} locales={capabilities.locales} handlers={handlers} t={t} />
      </div>
    </header>
    <div className="mx-auto grid max-w-[1600px] md:grid-cols-[220px_minmax(0,1fr)]">
      <aside className="hidden min-w-0 border-r border-line p-4 md:block"><nav aria-label={shellLabel(t, "ui.vault_navigation", "Vault navigation")}>{nav}</nav><div className="mt-8 break-words text-sm text-muted">{spend}</div></aside>
      <main id="main-content" tabIndex={-1} className="min-w-0 p-3 outline-none md:p-6">{children}</main>
    </div>
    <Dialog open={drawer} onOpenChange={setDrawer} title={shellLabel(t, "ui.navigation", "Navigation")} description={undefined} closeLabel={shellLabel(t, "ui.close_navigation", "Close navigation")}><nav aria-label={shellLabel(t, "ui.vault_navigation", "Vault navigation")}>{nav}</nav><div className="mt-5 text-sm text-muted">{spend}</div></Dialog>
  </div>;
}

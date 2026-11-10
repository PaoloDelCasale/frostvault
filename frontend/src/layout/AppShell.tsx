import { useMemo, useState, type ReactNode } from "react";
import type { QueryClient } from "@tanstack/react-query";
import { Menu, Snowflake } from "lucide-react";
import { createAppQueryClient } from "@/api";
import { AccountPreferencesMenu } from "@/components/AccountPreferencesMenu";
import { Button } from "@/components/ui/button";
import { NotificationCenter } from "@/components/NotificationCenter";
import { Dialog } from "@/components/Dialog";
import { shellLabel, type ShellTranslator } from "./labels";
import { ShellNavItems } from "./ShellNavItems";
import { VaultSwitcher } from "./VaultSwitcher";
import type { ShellCapabilities, ShellNavHandlers, ShellSection } from "./types";

/**
 * Header: brand, Vault switcher, upload activity, notifications, account.
 * Sidebar (drawer on phones): fixed destinations only.
 */
export function AppShell({ capabilities, handlers, t, queryClient: provided, children, problemCounts = {}, openProblems, section, activity, banner }: {
  capabilities: ShellCapabilities; handlers?: ShellNavHandlers; t?: ShellTranslator;
  queryClient?: QueryClient; children: ReactNode; problemCounts?: Record<number, number>; openProblems?: number;
  section?: ShellSection; activity?: ReactNode; banner?: ReactNode;
}) {
  const [drawer, setDrawer] = useState(false);
  const fallback = useMemo(() => createAppQueryClient(), []);
  const current = capabilities.vaults.find((vault) => vault.id === capabilities.currentVaultId);
  const navigateTo = (url: string) => { setDrawer(false); if (handlers?.onNavigate) handlers.onNavigate(url); else window.location.assign(url); };
  const nav = (onNavigate?: () => void) => <ShellNavItems section={section} currentVault={current} openProblems={openProblems} t={t} onNavigate={onNavigate} />;
  return <div className="min-h-svh bg-canvas text-ink">
    <a href="#main-content" className="skip-link" onClick={(event) => { event.preventDefault(); document.getElementById("main-content")?.focus(); }}>{shellLabel(t, "ui.skip_to_main", "Skip to main content")}</a>
    <header className="sticky top-0 z-40 border-b border-line bg-surface/95 px-3 py-2 backdrop-blur supports-[backdrop-filter]:bg-surface/85">
      <div className="mx-auto flex max-w-[1600px] min-w-0 flex-nowrap items-center gap-2" data-testid="app-shell-header-row">
        <Button className="min-h-11 min-w-11 shrink-0 md:hidden" variant="secondary" aria-label={shellLabel(t, "ui.open_navigation", "Open navigation")} onClick={() => setDrawer(true)}><Menu aria-hidden="true" /></Button>
        <a href="/" className="flex min-h-11 shrink-0 items-center gap-2 font-extrabold tracking-tight text-green no-underline" aria-label={shellLabel(t, "nav.home", "FrostVault home")}>
          <Snowflake className="size-5" aria-hidden="true" /><span className="hidden sm:inline">FrostVault</span>
        </a>
        <div className="flex min-w-0 flex-1 items-center sm:ml-2">
          {capabilities.vaults.length > 0 && <VaultSwitcher vaults={capabilities.vaults} currentVaultId={capabilities.currentVaultId} problemCounts={problemCounts} t={t} onNavigate={navigateTo} />}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {activity}
          <NotificationCenter locale={capabilities.locale} t={t} queryClient={provided ?? fallback} />
          <AccountPreferencesMenu locale={capabilities.locale} locales={capabilities.locales} handlers={handlers} t={t} />
        </div>
      </div>
    </header>
    <div className="mx-auto grid max-w-[1600px] md:grid-cols-[232px_minmax(0,1fr)]">
      <aside className="hidden min-w-0 border-r border-line md:block">
        <nav className="sticky top-[61px] p-3" aria-label={shellLabel(t, "ui.vault_navigation", "Main navigation")}>{nav()}</nav>
      </aside>
      <main id="main-content" tabIndex={-1} className="min-w-0 px-3 py-4 outline-none md:px-8 md:py-6">{banner}{children}</main>
    </div>
    <Dialog open={drawer} onOpenChange={setDrawer} title={shellLabel(t, "ui.navigation", "Navigation")} description={undefined} closeLabel={shellLabel(t, "ui.close_navigation", "Close navigation")}>
      <nav aria-label={shellLabel(t, "ui.vault_navigation", "Main navigation")}>{nav(() => setDrawer(false))}</nav>
    </Dialog>
  </div>;
}

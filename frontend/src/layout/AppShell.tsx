import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { QueryClient } from "@tanstack/react-query";

import { createAppQueryClient } from "@/api";
import { AccountPreferencesMenu } from "@/components/AccountPreferencesMenu";
import { Button } from "@/components/ui/button";
import { NotificationCenter } from "@/components/NotificationCenter";

import { AppDrawer } from "./AppDrawer";
import { shellLabel } from "./labels";
import { ShellNavItems } from "./ShellNavItems";
import type { ShellCapabilities, ShellNavHandlers } from "./types";

type AppShellProps = {
  capabilities: ShellCapabilities;
  handlers?: ShellNavHandlers;
  t?: (key: string, params?: Record<string, unknown>) => string;
  /** Shared app client; optional for isolated shell stories and tests. */
  queryClient?: QueryClient;
  /**
   * Keep the document shell mounted while fresh authentication authority is
   * being resolved, but do not render capabilities from the previous Session.
   */
  authReconciliationPending?: boolean;
  children: ReactNode;
};

export function AppShell({
  capabilities,
  handlers,
  t,
  queryClient: providedQueryClient,
  authReconciliationPending = false,
  children,
}: AppShellProps) {
  const [drawerOpen, setDrawerOpen] = useState(false);

  useEffect(() => {
    if (authReconciliationPending) setDrawerOpen(false);
  }, [authReconciliationPending]);
  const fallbackQueryClient = useMemo(() => createAppQueryClient(), []);
  const queryClient = providedQueryClient ?? fallbackQueryClient;
  const skipToMainLabel = shellLabel(
    t,
    "ui.skip_to_main",
    "Skip to main content",
  );
  const openNavigationLabel = shellLabel(
    t,
    "ui.open_navigation",
    "Open navigation",
  );
  const vaultNavigationLabel = shellLabel(
    t,
    "ui.vault_navigation",
    "Vault navigation",
  );
  const loadingLabel = shellLabel(t, "ui.loading", "Loading…");

  const accountHandlers = handlers
    ? {
        onNewVault: handlers.onNewVault,
        onAdministration: handlers.onAdministration,
        onSignOut: handlers.onSignOut,
        onLocaleChange: handlers.onLocaleChange,
      }
    : undefined;

  return (
    <div className="min-h-svh bg-canvas text-ink">
      <a
        href="#main-content"
        className="skip-link"
        onClick={(event) => {
          event.preventDefault();
          const main = document.getElementById("main-content");
          main?.focus();
        }}
      >
        {skipToMainLabel}
      </a>

      <header className="border-b border-line bg-surface px-4 py-3 pt-[max(0.75rem,env(safe-area-inset-top))]">
        <div
          data-testid="app-shell-header-row"
          className="mx-auto flex max-w-[1180px] flex-nowrap items-center justify-between gap-3"
        >
          <div className="min-w-0 flex-1">
            <p className="text-xs font-extrabold tracking-[0.16em] text-green uppercase">
              FrostVault
            </p>
            <h1 className="truncate text-xl font-bold tracking-tight md:text-2xl">
              {authReconciliationPending ? loadingLabel : capabilities.vaultName}
            </h1>
          </div>

          {!authReconciliationPending ? (
            <div className="flex shrink-0 flex-nowrap items-center gap-2">
              <nav
                aria-label={vaultNavigationLabel}
                className="hidden md:flex md:flex-nowrap md:items-center md:gap-2"
                data-testid="desktop-vault-nav"
              >
                <ShellNavItems
                  capabilities={capabilities}
                  handlers={handlers}
                  t={t}
                  density="primary"
                  className="flex flex-row flex-nowrap items-center gap-2"
                />
              </nav>

              <NotificationCenter
                currentVaultId={capabilities.currentVaultId}
                vaultName={capabilities.vaultName}
                locale={capabilities.locale}
                t={t}
                queryClient={queryClient}
              />

              <AccountPreferencesMenu
                locale={capabilities.locale}
                locales={capabilities.locales}
                isAdmin={capabilities.isAdmin}
                handlers={accountHandlers}
                t={t}
              />

              <div className="md:hidden">
                <AppDrawer
                  open={drawerOpen}
                  onOpenChange={setDrawerOpen}
                  capabilities={capabilities}
                  handlers={handlers}
                  t={t}
                  trigger={
                    <Button
                      type="button"
                      variant="secondary"
                      className="min-h-11 min-w-11"
                      aria-label={openNavigationLabel}
                    >
                      ☰
                    </Button>
                  }
                />
              </div>
            </div>
          ) : null}
        </div>
      </header>

      <main
        id="main-content"
        tabIndex={-1}
        className="mx-auto w-[min(1180px,calc(100%-2rem))] py-6 outline-none"
      >
        {children}
      </main>
    </div>
  );
}

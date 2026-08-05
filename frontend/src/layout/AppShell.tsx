import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { QueryClient } from "@tanstack/react-query";

import { createAppQueryClient } from "@/api";
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

      <header
        className="border-b border-line bg-surface px-4 py-3 pt-[max(0.75rem,env(safe-area-inset-top))]"
      >
        <div className="mx-auto flex max-w-[1180px] items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs font-extrabold tracking-[0.16em] text-green uppercase">
              FrostVault
            </p>
            <h1 className="truncate text-xl font-bold tracking-tight md:text-2xl">
              {authReconciliationPending ? loadingLabel : capabilities.vaultName}
            </h1>
          </div>

          {!authReconciliationPending ? (
            <div className="flex items-center gap-2">
              <NotificationCenter
                currentVaultId={capabilities.currentVaultId}
                vaultName={capabilities.vaultName}
                locale={capabilities.locale}
                t={t}
                queryClient={queryClient}
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

              <nav
                aria-label={vaultNavigationLabel}
                className="hidden md:flex md:flex-wrap md:items-center md:justify-end md:gap-2"
              >
                <ShellNavItems
                  capabilities={capabilities}
                  handlers={handlers}
                  t={t}
                  className="flex flex-row flex-wrap items-end gap-2"
                />
              </nav>
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

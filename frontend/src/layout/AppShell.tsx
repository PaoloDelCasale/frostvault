import { useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";

import { AppDrawer } from "./AppDrawer";
import { ShellNavItems } from "./ShellNavItems";
import type { ShellCapabilities, ShellNavHandlers } from "./types";

type AppShellProps = {
  capabilities: ShellCapabilities;
  handlers?: ShellNavHandlers;
  t?: (key: string) => string;
  children: ReactNode;
};

export function AppShell({ capabilities, handlers, t, children }: AppShellProps) {
  const [drawerOpen, setDrawerOpen] = useState(false);

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
        Skip to main content
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
              {capabilities.vaultName}
            </h1>
          </div>

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
                  aria-label="Open navigation"
                >
                  ☰
                </Button>
              }
            />
          </div>

          <nav
            aria-label="Vault navigation"
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

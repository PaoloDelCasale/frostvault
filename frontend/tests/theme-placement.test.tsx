import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import { I18nContext, type I18nContextValue } from "@/i18n/context";
import { translate } from "@/i18n/translate";
import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities, ShellNavHandlers } from "@/layout/types";
import { LoginPage } from "@/pages/login/LoginPage";
import { NoVaultPage } from "@/pages/no-vault/NoVaultPage";
import { VaultCreatePage } from "@/pages/vault-create";
import { ThemeProvider } from "@/theme";

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/locales",
);

function loadCatalog(locale: "en" | "it"): Record<string, string> {
  return JSON.parse(
    readFileSync(path.join(localesDir, `${locale}.json`), "utf8"),
  ) as Record<string, string>;
}

const shellCapabilities: ShellCapabilities = {
  vaultName: "Test Archive",
  isVaultOwner: true,
  canOperate: true,
  isAdmin: true,
  locale: "en",
  locales: ["en", "it"],
  vaults: [{ id: 1, slug: "test", name: "Test Archive", role: "owner" }],
  currentVaultId: 1,
};

const shellHandlers: ShellNavHandlers = {
  onNewVault: vi.fn(),
  onManageAccess: vi.fn(),
  onAdministration: vi.fn(),
  onSignOut: vi.fn(),
  onLocaleChange: vi.fn(),
  onVaultChange: vi.fn(),
};

function renderWithProviders(
  ui: ReactNode,
  locale: "en" | "it" = "en",
) {
  const catalog = loadCatalog(locale);
  const value: I18nContextValue = {
    locale,
    locales: ["en", "it"],
    ready: true,
    t: (key, params) => translate(catalog, key, params),
    setLocale: async () => undefined,
  };
  const client = createAppQueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    catalog,
    user: userEvent.setup(),
    ...render(
      <ApiQueryProvider client={client}>
        <I18nContext.Provider value={value}>
          <ThemeProvider>{ui}</ThemeProvider>
        </I18nContext.Provider>
      </ApiQueryProvider>,
    ),
  };
}

describe("theme control placement (#223 / #224)", () => {
  afterEach(() => {
    cleanup();
    resetApiClientForTests();
  });

  it("keeps a single Appearance control behind the shell account menu", async () => {
    const catalog = loadCatalog("en");
    const { user } = renderWithProviders(
      <AppShell
        capabilities={shellCapabilities}
        handlers={shellHandlers}
        t={(key) => catalog[key] ?? key}
      >
        <p>Archive content</p>
      </AppShell>,
    );

    const header = screen.getByRole("banner");
    expect(
      within(header).queryByTestId("theme-control"),
    ).not.toBeInTheDocument();
    expect(screen.queryAllByTestId("theme-control")).toHaveLength(0);

    const openAccount = screen.getByRole("button", {
      name: catalog["ui.open_account_menu"],
    });
    expect(openAccount).toHaveClass("min-h-11");
    expect(
      screen.getAllByRole("button", { name: catalog["ui.open_account_menu"] }),
    ).toHaveLength(1);

    await user.click(openAccount);

    const account = await screen.findByRole("dialog", {
      name: catalog["ui.account_menu"],
    });
    expect(
      within(account).getAllByTestId("theme-control"),
    ).toHaveLength(1);
    expect(
      within(account).getByRole("combobox", {
        name: catalog["ui.theme"],
      }),
    ).toBeInTheDocument();
    expect(
      within(account).getByRole("option", {
        name: catalog["ui.theme_system"],
      }),
    ).toBeInTheDocument();
    expect(
      within(account).getByRole("option", {
        name: catalog["ui.theme_light"],
      }),
    ).toBeInTheDocument();
    expect(
      within(account).getByRole("option", {
        name: catalog["ui.theme_dark"],
      }),
    ).toBeInTheDocument();
  });

  it("does not put Appearance in the mobile drawer navigation list", async () => {
    const catalog = loadCatalog("en");
    const { user } = renderWithProviders(
      <AppShell
        capabilities={shellCapabilities}
        handlers={shellHandlers}
        t={(key) => catalog[key] ?? key}
      >
        <p>Archive content</p>
      </AppShell>,
    );

    // Account menu stays in the shell chrome, not inside the navigation drawer.
    expect(
      screen.getByRole("button", { name: catalog["ui.open_account_menu"] }),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: catalog["ui.open_navigation"] }),
    );
    const drawer = await screen.findByRole("dialog", {
      name: catalog["ui.navigation"],
    });

    expect(within(drawer).queryByTestId("theme-control")).not.toBeInTheDocument();
    expect(
      within(drawer).queryByRole("combobox", { name: catalog["ui.theme"] }),
    ).not.toBeInTheDocument();
    expect(
      within(drawer).queryByRole("button", {
        name: catalog["ui.open_account_menu"],
      }),
    ).not.toBeInTheDocument();
    expect(
      within(drawer).queryByRole("button", {
        name: catalog["ui.open_preferences"],
      }),
    ).not.toBeInTheDocument();
  });

  it("exposes Italian account-menu appearance labels from the shell entry point", async () => {
    const catalog = loadCatalog("it");
    const { user } = renderWithProviders(
      <AppShell
        capabilities={{ ...shellCapabilities, locale: "it" }}
        handlers={shellHandlers}
        t={(key) => catalog[key] ?? key}
      >
        <p>Contenuto archivio</p>
      </AppShell>,
      "it",
    );

    await user.click(
      screen.getByRole("button", { name: catalog["ui.open_account_menu"] }),
    );
    const account = await screen.findByRole("dialog", {
      name: catalog["ui.account_menu"],
    });
    expect(
      within(account).getByRole("combobox", {
        name: catalog["ui.theme"],
      }),
    ).toBeInTheDocument();
    expect(
      within(account).getByRole("option", {
        name: catalog["ui.theme_dark"],
      }),
    ).toBeInTheDocument();
  });

  it("keeps at most one compact Appearance control on the sign-in screen", () => {
    const { catalog } = renderWithProviders(<LoginPage />);

    expect(screen.getAllByTestId("theme-control")).toHaveLength(1);
    expect(
      screen.getByRole("combobox", { name: catalog["ui.theme"] }),
    ).toHaveClass("min-h-11");
    expect(
      screen.queryByRole("button", { name: catalog["ui.open_preferences"] }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: catalog["ui.open_account_menu"] }),
    ).not.toBeInTheDocument();
  });

  it("moves Appearance out of the no-Vault page body into preferences", async () => {
    const { catalog, user } = renderWithProviders(<NoVaultPage />);

    expect(screen.queryByTestId("theme-control")).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: catalog["ui.open_preferences"] }),
    );
    expect(await screen.findAllByTestId("theme-control")).toHaveLength(1);
  });

  it("moves Appearance out of the Vault creation page body into preferences", async () => {
    const { catalog, user } = renderWithProviders(
      <VaultCreatePage displayName="Ada Lovelace" />,
    );

    expect(screen.queryByTestId("theme-control")).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: catalog["ui.open_preferences"] }),
    );
    expect(await screen.findAllByTestId("theme-control")).toHaveLength(1);
  });
});

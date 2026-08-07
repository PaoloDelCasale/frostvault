import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

import {
  ApiQueryProvider,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import { I18nContext, type I18nContextValue } from "@/i18n/context";
import { translate } from "@/i18n/translate";
import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities } from "@/layout/types";
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

describe("theme control placement (#223)", () => {
  afterEach(() => {
    cleanup();
    resetApiClientForTests();
  });

  it("keeps a single Appearance control behind the shell preferences surface", async () => {
    const catalog = loadCatalog("en");
    const { user } = renderWithProviders(
      <AppShell capabilities={shellCapabilities} t={(key) => catalog[key] ?? key}>
        <p>Archive content</p>
      </AppShell>,
    );

    const header = screen.getByRole("banner");
    expect(
      within(header).queryByTestId("theme-control"),
    ).not.toBeInTheDocument();
    expect(screen.queryAllByTestId("theme-control")).toHaveLength(0);

    const openPreferences = screen.getByRole("button", {
      name: catalog["ui.open_preferences"],
    });
    expect(openPreferences).toHaveClass("min-h-11");
    expect(
      screen.getAllByRole("button", { name: catalog["ui.open_preferences"] }),
    ).toHaveLength(1);

    await user.click(openPreferences);

    const preferences = await screen.findByRole("dialog", {
      name: catalog["ui.preferences"],
    });
    expect(
      within(preferences).getAllByTestId("theme-control"),
    ).toHaveLength(1);
    expect(
      within(preferences).getByRole("combobox", {
        name: catalog["ui.theme"],
      }),
    ).toBeInTheDocument();
    expect(
      within(preferences).getByRole("option", {
        name: catalog["ui.theme_system"],
      }),
    ).toBeInTheDocument();
    expect(
      within(preferences).getByRole("option", {
        name: catalog["ui.theme_light"],
      }),
    ).toBeInTheDocument();
    expect(
      within(preferences).getByRole("option", {
        name: catalog["ui.theme_dark"],
      }),
    ).toBeInTheDocument();
  });

  it("does not put Appearance in the mobile drawer navigation list", async () => {
    const catalog = loadCatalog("en");
    const { user } = renderWithProviders(
      <AppShell capabilities={shellCapabilities} t={(key) => catalog[key] ?? key}>
        <p>Archive content</p>
      </AppShell>,
    );

    // Preferences stay in the shell chrome, not inside the navigation drawer.
    expect(
      screen.getByRole("button", { name: catalog["ui.open_preferences"] }),
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
        name: catalog["ui.open_preferences"],
      }),
    ).not.toBeInTheDocument();
  });

  it("exposes Italian preferences labels from the shell entry point", async () => {
    const catalog = loadCatalog("it");
    const { user } = renderWithProviders(
      <AppShell
        capabilities={{ ...shellCapabilities, locale: "it" }}
        t={(key) => catalog[key] ?? key}
      >
        <p>Contenuto archivio</p>
      </AppShell>,
      "it",
    );

    await user.click(
      screen.getByRole("button", { name: catalog["ui.open_preferences"] }),
    );
    const preferences = await screen.findByRole("dialog", {
      name: catalog["ui.preferences"],
    });
    expect(
      within(preferences).getByRole("combobox", {
        name: catalog["ui.theme"],
      }),
    ).toBeInTheDocument();
    expect(
      within(preferences).getByRole("option", {
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

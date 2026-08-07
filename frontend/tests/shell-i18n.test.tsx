import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities, ShellNavHandlers } from "@/layout/types";

type Catalog = Record<string, string>;

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/locales",
);

function loadCatalog(locale: "en" | "it"): Catalog {
  return JSON.parse(
    readFileSync(path.join(localesDir, `${locale}.json`), "utf8"),
  ) as Catalog;
}

const shellCapabilities: ShellCapabilities = {
  vaultName: "Test Archive",
  isVaultOwner: true,
  canOperate: true,
  isAdmin: true,
  locale: "en",
  locales: ["en", "it"],
  vaults: [{ id: 1, slug: "test", name: "Test Archive", role: "owner" }],
};

const shellHandlers: ShellNavHandlers = {
  onNewVault: vi.fn(),
  onManageAccess: vi.fn(),
  onAdministration: vi.fn(),
  onSignOut: vi.fn(),
  onLocaleChange: vi.fn(),
  onVaultChange: vi.fn(),
};

async function openDrawer(locale: "en" | "it") {
  const catalog = loadCatalog(locale);
  const user = userEvent.setup();
  render(
    <AppShell
      capabilities={{ ...shellCapabilities, locale }}
      handlers={shellHandlers}
      t={(key) => catalog[key] ?? key}
    >
      <p>Archive content</p>
    </AppShell>,
  );
  await user.click(
    screen.getByRole("button", { name: catalog["ui.open_navigation"] }),
  );
  return {
    catalog,
    user,
    dialog: await screen.findByRole("dialog", {
      name: catalog["ui.navigation"],
    }),
  };
}

async function openAccountMenu(
  user: ReturnType<typeof userEvent.setup>,
  catalog: Catalog,
) {
  // The vault drawer is modal and aria-hides the header account trigger.
  const close = screen.queryByRole("button", {
    name: catalog["ui.close_navigation"],
  });
  if (close) {
    await user.click(close);
    expect(
      screen.queryByRole("dialog", { name: catalog["ui.navigation"] }),
    ).not.toBeInTheDocument();
  }
  await user.click(
    screen.getByRole("button", { name: catalog["ui.open_account_menu"] }),
  );
  return screen.findByRole("dialog", { name: catalog["ui.account_menu"] });
}

describe("shell catalog labels", () => {
  it("keeps vault-primary drawer labels catalog-backed without secondary chrome", async () => {
    const { catalog, dialog, user } = await openDrawer("en");
    const drawer = within(dialog);

    expect(dialog).toHaveTextContent(catalog["ui.manage_access"]);
    expect(dialog).not.toHaveTextContent(catalog["ui.new_vault"]);
    expect(dialog).not.toHaveTextContent(catalog["ui.administration"]);
    expect(dialog).not.toHaveTextContent(catalog["ui.sign_out"]);
    expect(dialog).not.toHaveTextContent(
      catalog["ui.refresh_list"] ?? "Refresh list",
    );
    expect(dialog).toHaveAccessibleName(catalog["ui.navigation"]);
    expect(
      drawer.getByRole("button", { name: catalog["ui.close_navigation"] }),
    ).toBeInTheDocument();
    expect(
      drawer.getByRole("combobox", { name: catalog["ui.vault"] }),
    ).toBeInTheDocument();
    expect(
      drawer.queryByRole("combobox", { name: catalog["ui.language"] }),
    ).not.toBeInTheDocument();

    const account = await openAccountMenu(user, catalog);
    expect(account).toHaveTextContent(catalog["ui.new_vault"]);
    expect(account).toHaveTextContent(catalog["ui.administration"]);
    expect(account).toHaveTextContent(catalog["ui.sign_out"]);
    expect(
      within(account).getByRole("combobox", { name: catalog["ui.language"] }),
    ).toBeInTheDocument();
    expect(
      within(account).getByRole("combobox", { name: catalog["ui.theme"] }),
    ).toBeInTheDocument();
    expect(account).not.toHaveTextContent(catalog["ui.manage_access"]);
  });

  it("renders Italian vault-primary and account-menu labels without Refresh list", async () => {
    const { catalog, dialog, user } = await openDrawer("it");
    const drawer = within(dialog);

    expect(dialog).toHaveTextContent(catalog["ui.manage_access"]);
    expect(dialog).not.toHaveTextContent(catalog["ui.new_vault"]);
    expect(dialog).not.toHaveTextContent(
      catalog["ui.refresh_list"] ?? "Aggiorna elenco",
    );
    expect(
      drawer.getByRole("button", { name: catalog["ui.close_navigation"] }),
    ).toBeInTheDocument();
    expect(
      drawer.getByRole("combobox", { name: catalog["ui.vault"] }),
    ).toBeInTheDocument();

    const account = await openAccountMenu(user, catalog);
    expect(account).toHaveTextContent(catalog["ui.new_vault"]);
    expect(account).toHaveTextContent(catalog["ui.sign_out"]);
    expect(
      within(account).getByRole("combobox", { name: catalog["ui.language"] }),
    ).toBeInTheDocument();
    expect(account).not.toHaveTextContent(catalog["ui.manage_access"]);
  });
});

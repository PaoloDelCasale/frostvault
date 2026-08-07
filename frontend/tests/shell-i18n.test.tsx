import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities } from "@/layout/types";

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

async function openDrawer(locale: "en" | "it") {
  const catalog = loadCatalog(locale);
  const user = userEvent.setup();
  render(
    <AppShell
      capabilities={{ ...shellCapabilities, locale }}
      t={(key) => catalog[key] ?? key}
    >
      <p>Archive content</p>
    </AppShell>,
  );
  await user.click(
    screen.getByRole("button", { name: catalog["ui.open_navigation"] }),
  );
  return { catalog, dialog: await screen.findByRole("dialog") };
}

describe("shell catalog labels", () => {
  it("keeps the English shell labels and aria labels catalog-backed", async () => {
    const { catalog, dialog } = await openDrawer("en");
    const drawer = within(dialog);

    expect(dialog).toHaveTextContent(catalog["ui.new_vault"]);
    expect(dialog).toHaveTextContent(catalog["ui.manage_access"]);
    expect(dialog).not.toHaveTextContent(catalog["ui.refresh_list"] ?? "Refresh list");
    expect(dialog).toHaveTextContent(catalog["ui.administration"]);
    expect(dialog).toHaveTextContent(catalog["ui.sign_out"]);
    expect(dialog).toHaveAccessibleName(catalog["ui.navigation"]);
    expect(
      drawer.getByRole("button", { name: catalog["ui.close_navigation"] }),
    ).toBeInTheDocument();
    expect(
      drawer.getByRole("combobox", { name: catalog["ui.vault"] }),
    ).toBeInTheDocument();
    expect(
      drawer.getByRole("combobox", { name: catalog["ui.language"] }),
    ).toBeInTheDocument();
  });

  it("renders the new vault and manage access labels in Italian without Refresh list", async () => {
    const { catalog, dialog } = await openDrawer("it");
    const drawer = within(dialog);

    expect(dialog).toHaveTextContent(catalog["ui.new_vault"]);
    expect(dialog).toHaveTextContent(catalog["ui.manage_access"]);
    expect(dialog).not.toHaveTextContent(catalog["ui.refresh_list"] ?? "Aggiorna elenco");
    expect(dialog).toHaveTextContent(catalog["ui.sign_out"]);
    expect(
      drawer.getByRole("button", { name: catalog["ui.close_navigation"] }),
    ).toBeInTheDocument();
    expect(
      drawer.getByRole("combobox", { name: catalog["ui.vault"] }),
    ).toBeInTheDocument();
    expect(
      drawer.getByRole("combobox", { name: catalog["ui.language"] }),
    ).toBeInTheDocument();
  });
});

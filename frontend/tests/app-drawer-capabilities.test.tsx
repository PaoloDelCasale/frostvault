import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities, ShellNavHandlers } from "@/layout/types";

const shellHandlers: ShellNavHandlers = {
  onNewVault: vi.fn(),
  onManageAccess: vi.fn(),
  onAdministration: vi.fn(),
  onSignOut: vi.fn(),
  onLocaleChange: vi.fn(),
  onVaultChange: vi.fn(),
};

async function renderShell(
  capabilities: ShellCapabilities,
  t?: (key: string) => string,
) {
  const user = userEvent.setup();
  render(
    <AppShell capabilities={capabilities} handlers={shellHandlers} t={t}>
      <p>content</p>
    </AppShell>,
  );
  return user;
}

async function openDrawer(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /open navigation/i }));
  return screen.findByRole("dialog", { name: /navigation/i });
}

async function closeDrawerIfOpen(user: ReturnType<typeof userEvent.setup>) {
  const close = screen.queryByRole("button", { name: /close navigation/i });
  if (close) {
    await user.click(close);
    expect(screen.queryByRole("dialog", { name: /navigation/i })).not.toBeInTheDocument();
  }
}

async function openAccountMenu(user: ReturnType<typeof userEvent.setup>) {
  // The vault drawer is modal and aria-hides the header account trigger.
  await closeDrawerIfOpen(user);
  await user.click(screen.getByRole("button", { name: /open account menu/i }));
  return screen.findByRole("dialog", { name: /^account$/i });
}

describe("App drawer capability filtering", () => {
  it("shows Manage access for an owner in vault nav, not Administration or Refresh list", async () => {
    const user = await renderShell({
      vaultName: "Owner Vault",
      isVaultOwner: true,
      canOperate: false,
      isAdmin: false,
      locale: "en",
      locales: ["en"],
      vaults: [{ id: 1, slug: "owner", name: "Owner Vault", role: "owner" }],
      role: "owner",
    });

    const drawer = await openDrawer(user);
    expect(drawer).toHaveTextContent("Manage access");
    expect(drawer).not.toHaveTextContent("Administration");
    expect(drawer).not.toHaveTextContent("Refresh list");
    expect(drawer).not.toHaveTextContent("New vault");
    expect(drawer).not.toHaveTextContent("Sign out");
    expect(drawer).not.toHaveTextContent("Language");

    const account = await openAccountMenu(user);
    expect(account).toHaveTextContent("New vault");
    expect(account).toHaveTextContent("Sign out");
    expect(account).toHaveTextContent("Language");
    expect(account).not.toHaveTextContent("Administration");
    expect(account).not.toHaveTextContent("Manage access");
    expect(account).not.toHaveTextContent("Refresh list");
  });

  it("hides Manage access, Administration and Refresh list for an operator", async () => {
    const user = await renderShell({
      vaultName: "Ops Vault",
      isVaultOwner: false,
      canOperate: true,
      isAdmin: false,
      locale: "en",
      locales: ["en"],
      vaults: [{ id: 2, slug: "ops", name: "Ops Vault", role: "operator" }],
      role: "operator",
    });

    const drawer = await openDrawer(user);
    expect(drawer).not.toHaveTextContent("Refresh list");
    expect(drawer).not.toHaveTextContent("Aggiorna elenco");
    expect(drawer).not.toHaveTextContent("Manage access");
    expect(drawer).not.toHaveTextContent("Administration");
    expect(drawer).not.toHaveTextContent("New vault");
    expect(drawer).not.toHaveTextContent("Sign out");

    const account = await openAccountMenu(user);
    expect(account).toHaveTextContent("New vault");
    expect(account).toHaveTextContent("Sign out");
    expect(account).not.toHaveTextContent("Manage access");
    expect(account).not.toHaveTextContent("Administration");
    expect(account).not.toHaveTextContent("Refresh list");
  });

  it("hides Manage access, Administration and Refresh list for a viewer", async () => {
    const user = await renderShell({
      vaultName: "View Vault",
      isVaultOwner: false,
      canOperate: false,
      isAdmin: false,
      locale: "en",
      locales: ["en"],
      vaults: [{ id: 3, slug: "view", name: "View Vault", role: "viewer" }],
      role: "viewer",
    });

    const drawer = await openDrawer(user);
    expect(drawer).not.toHaveTextContent("Manage access");
    expect(drawer).not.toHaveTextContent("Administration");
    expect(drawer).not.toHaveTextContent("Refresh list");

    const account = await openAccountMenu(user);
    expect(account).toHaveTextContent("New vault");
    expect(account).toHaveTextContent("Language");
    expect(account).toHaveTextContent("Sign out");
    expect(account).not.toHaveTextContent("Manage access");
    expect(account).not.toHaveTextContent("Administration");
  });

  it("shows Administration for an admin only in the account menu", async () => {
    const user = await renderShell({
      vaultName: "Admin Vault",
      isVaultOwner: false,
      canOperate: false,
      isAdmin: true,
      locale: "en",
      locales: ["en"],
      vaults: [{ id: 4, slug: "admin", name: "Admin Vault", role: "viewer" }],
      role: "viewer",
    });

    const drawer = await openDrawer(user);
    expect(drawer).not.toHaveTextContent("Administration");
    expect(drawer).not.toHaveTextContent("Manage access");
    expect(drawer).not.toHaveTextContent("Refresh list");

    const account = await openAccountMenu(user);
    expect(account).toHaveTextContent("Administration");
    expect(account).not.toHaveTextContent("Manage access");
    expect(account).not.toHaveTextContent("Refresh list");
  });

  it("keeps desktop primary header free of secondary destinations", async () => {
    const user = await renderShell({
      vaultName: "Very Long Owner Archive Name For Header",
      isVaultOwner: true,
      canOperate: true,
      isAdmin: true,
      locale: "en",
      locales: ["en", "it"],
      vaults: [
        {
          id: 1,
          slug: "long",
          name: "Very Long Owner Archive Name For Header",
          role: "owner",
        },
      ],
      role: "owner",
      currentVaultId: 1,
    });

    const header = screen.getByRole("banner");
    const headerRow = within(header).getByTestId("app-shell-header-row");
    expect(headerRow.className).toMatch(/flex-nowrap/);

    // Secondary destinations must not sit permanently in the header chrome.
    expect(
      within(header).queryByRole("button", { name: /^new vault$/i }),
    ).not.toBeInTheDocument();
    expect(
      within(header).queryByRole("button", { name: /^administration$/i }),
    ).not.toBeInTheDocument();
    expect(
      within(header).queryByRole("button", { name: /^sign out$/i }),
    ).not.toBeInTheDocument();
    expect(
      within(header).queryByRole("combobox", { name: /^language$/i }),
    ).not.toBeInTheDocument();
    expect(header).not.toHaveTextContent("Refresh list");

    // Primary / vault-contextual controls remain reachable from the shell.
    expect(
      within(header).getByRole("button", { name: /open account menu/i }),
    ).toBeInTheDocument();
    expect(
      within(header).getByTestId("notification-bell"),
    ).toBeInTheDocument();

    const account = await openAccountMenu(user);
    expect(
      within(account).getByRole("button", { name: /^new vault$/i }),
    ).toBeInTheDocument();
    expect(
      within(account).getByRole("button", { name: /^administration$/i }),
    ).toBeInTheDocument();
    expect(
      within(account).getByRole("combobox", { name: /^language$/i }),
    ).toBeInTheDocument();
    expect(
      within(account).getByRole("combobox", { name: /^appearance$/i }),
    ).toBeInTheDocument();
    expect(
      within(account).getByRole("button", { name: /^sign out$/i }),
    ).toBeInTheDocument();
    expect(account).not.toHaveTextContent("Manage access");
  });
});

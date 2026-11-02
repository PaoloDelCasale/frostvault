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
  it("shows Vault settings in vault nav and Administration in the account menu", async () => {
    const user = await renderShell({
      vaultName: "Owner Vault",
      locale: "en",
      locales: ["en"],
      vaults: [{ id: 1, slug: "owner", name: "Owner Vault" }],
    });

    const drawer = await openDrawer(user);
    expect(drawer).toHaveTextContent("Vault settings");
    expect(drawer).not.toHaveTextContent("Administration");
    expect(drawer).not.toHaveTextContent("Refresh list");
    expect(drawer).not.toHaveTextContent("New vault");
    expect(drawer).not.toHaveTextContent("Sign out");
    expect(drawer).not.toHaveTextContent("Language");

    const account = await openAccountMenu(user);
    expect(account).toHaveTextContent("New vault");
    expect(account).toHaveTextContent("Administration");
    expect(account).toHaveTextContent("Sign out");
    expect(account).toHaveTextContent("Language");
    expect(account).not.toHaveTextContent("Vault settings");
    expect(account).not.toHaveTextContent("Refresh list");
  });

  it("keeps desktop primary header free of secondary destinations", async () => {
    const user = await renderShell({
      vaultName: "Very Long Owner Archive Name For Header",
      locale: "en",
      locales: ["en", "it"],
      vaults: [
        {
          id: 1,
          slug: "long",
          name: "Very Long Owner Archive Name For Header",
        },
      ],
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

  it("constrains the desktop Vault select for long names at narrow desktop widths", async () => {
    const longName =
      "Very Long Owner Archive Name For Desktop Header Overflow Regression";
    const user = await renderShell({
      vaultName: longName,
      locale: "en",
      locales: ["en"],
      vaults: [
        { id: 1, slug: "long", name: longName },
        { id: 2, slug: "docs", name: "Docs" },
      ],
      currentVaultId: 1,
    });

    // Representative narrow desktop width (md breakpoint floor).
    const shell = screen.getByRole("banner").parentElement;
    expect(shell).not.toBeNull();
    Object.defineProperty(shell as HTMLElement, "clientWidth", {
      configurable: true,
      value: 768,
    });

    const desktopNav = screen.getByTestId("desktop-vault-nav");
    const desktopSelect = within(desktopNav).getByRole("combobox", {
      name: /^vault$/i,
    });

    // Inside the shrink-0 / nowrap primary cluster the select must carry an
    // explicit ceiling so a content-sized long name cannot expand the header.
    const desktopClasses = desktopSelect.className.split(/\s+/);
    expect(desktopClasses).toEqual(
      expect.arrayContaining([
        "min-w-0",
        expect.stringMatching(/^max-w-/),
      ]),
    );
    expect(desktopSelect).toHaveTextContent(longName);
    await user.click(desktopSelect);
    const desktopList = await screen.findByRole("listbox");
    expect(
      within(desktopList).getByRole("option", { name: longName }),
    ).toBeInTheDocument();
    // Ordinary short names remain first-class options in the same control.
    expect(
      within(desktopList).getByRole("option", { name: "Docs" }),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");

    // Mobile stacked drawer select stays unconstrained by the desktop clamp.
    const drawer = await openDrawer(user);
    const drawerSelect = within(drawer).getByRole("combobox", {
      name: /^vault$/i,
    });
    const drawerClasses = drawerSelect.className.split(/\s+/);
    expect(drawerClasses).not.toEqual(
      expect.arrayContaining([expect.stringMatching(/^max-w-/)]),
    );
    expect(drawerSelect).toHaveTextContent(longName);
    await user.click(drawerSelect);
    expect(
      await screen.findByRole("option", { name: longName }),
    ).toBeInTheDocument();
  });
});

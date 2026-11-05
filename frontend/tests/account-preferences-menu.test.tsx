import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AccountPreferencesMenu } from "@/components/AccountPreferencesMenu";
import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities, ShellNavHandlers } from "@/layout/types";

const shellCapabilities: ShellCapabilities = {
  vaultName: "Owner Admin Archive",
  locale: "en",
  locales: ["en", "it"],
  vaults: [
    {
      id: 1,
      slug: "owner-admin",
      name: "Owner Admin Archive",
    },
  ],
  currentVaultId: 1,
};

const shellHandlers: ShellNavHandlers = {
  onNewVault: vi.fn(),
  onGeneral: vi.fn(),
  onSignOut: vi.fn(),
  onLocaleChange: vi.fn(),
  onVaultChange: vi.fn(),
};

afterEach(() => {
  cleanup();
});

describe("AccountPreferencesMenu focus restore (#224)", () => {
  it("returns focus to the trigger after Escape closes the shell account menu", async () => {
    const user = userEvent.setup();
    render(
      <AccountPreferencesMenu
        locale="en"
        locales={["en", "it"]}
        handlers={{
          onNewVault: vi.fn(),
          onGeneral: vi.fn(),
          onSignOut: vi.fn(),
          onLocaleChange: vi.fn(),
        }}
      />,
    );

    const trigger = screen.getByRole("button", { name: /open account menu/i });
    await user.click(trigger);

    const dialog = await screen.findByRole("dialog", { name: /^account$/i });
    expect(
      within(dialog).getByRole("button", { name: /sign out/i }),
    ).toBeInTheDocument();

    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: /^account$/i })).not.toBeInTheDocument();
    });
    expect(trigger).toHaveFocus();
  });

  it("returns focus to the trigger after the close control dismisses the menu", async () => {
    const user = userEvent.setup();
    render(
      <AccountPreferencesMenu
        locale="en"
        locales={["en"]}
        handlers={{
          onNewVault: vi.fn(),
          onSignOut: vi.fn(),
        }}
      />,
    );

    const trigger = screen.getByRole("button", { name: /open account menu/i });
    await user.click(trigger);

    const dialog = await screen.findByRole("dialog", { name: /^account$/i });
    await user.click(
      within(dialog).getByRole("button", { name: /close account menu/i }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: /^account$/i })).not.toBeInTheDocument();
    });
    expect(trigger).toHaveFocus();
  });
});

describe("AppShell desktop header single-row alignment (#224)", () => {
  it("top-aligns the header row so brand and primary controls share one row", () => {
    render(
      <AppShell capabilities={shellCapabilities} handlers={shellHandlers}>
        <p>content</p>
      </AppShell>,
    );

    const headerRow = screen.getByTestId("app-shell-header-row");
    // items-center vertically offsets unequal-height children and fails the
    // desktop uniqueTops===1 a11y guard; top alignment keeps one geometric row.
    expect(headerRow.className.split(/\s+/)).toEqual(
      expect.arrayContaining(["flex-nowrap", "items-center"]),
    );


    const header = screen.getByRole("banner");
    expect(screen.getByRole("navigation", { name: /vault navigation/i })).toBeInTheDocument();
    expect(within(header).getByTestId("notification-bell")).toBeInTheDocument();
    expect(
      within(header).getByRole("button", { name: /open account menu/i }),
    ).toBeInTheDocument();
    expect(
      within(header).queryByRole("button", { name: /^new vault$/i }),
    ).not.toBeInTheDocument();
    expect(
      within(header).queryByRole("button", { name: /^general$/i }),
    ).not.toBeInTheDocument();
  });
});

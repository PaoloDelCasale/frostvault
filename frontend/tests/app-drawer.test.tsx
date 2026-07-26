import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities } from "@/layout/types";

const viewerCaps: ShellCapabilities = {
  vaultName: "Test Archive",
  isVaultOwner: false,
  canOperate: false,
  isAdmin: false,
  locale: "en",
  locales: ["en", "it"],
  vaults: [{ id: 1, slug: "test", name: "Test Archive", role: "viewer" }],
};

describe("App drawer", () => {
  it("opens and closes; while open focus stays inside; Esc returns focus to the hamburger", async () => {
    const user = userEvent.setup();
    render(
      <AppShell capabilities={viewerCaps}>
        <p>Archive content</p>
      </AppShell>,
    );

    const hamburger = screen.getByRole("button", { name: /open navigation/i });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(hamburger);

    const drawer = await screen.findByRole("dialog");
    expect(drawer).toBeInTheDocument();
    await waitFor(() => {
      expect(drawer.contains(document.activeElement)).toBe(true);
    });

    await user.tab();
    expect(drawer.contains(document.activeElement)).toBe(true);

    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(hamburger).toHaveFocus();
  });
});

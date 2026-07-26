import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities } from "@/layout/types";

const caps: ShellCapabilities = {
  vaultName: "Test Archive",
  isVaultOwner: false,
  canOperate: false,
  isAdmin: false,
  locale: "en",
  locales: ["en"],
  vaults: [{ id: 1, slug: "test", name: "Test Archive", role: "viewer" }],
};

describe("Skip link", () => {
  it("is the first focusable element and moves focus to the main content", async () => {
    const user = userEvent.setup();
    render(
      <AppShell capabilities={caps}>
        <p>Archive body</p>
      </AppShell>,
    );

    await user.tab();
    const skip = screen.getByRole("link", { name: /skip to main content/i });
    expect(skip).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(document.getElementById("main-content")).toHaveFocus();
  });
});

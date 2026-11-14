import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderVaultAccess } from "./vault-settings-harness";

afterEach(() => { window.history.replaceState({}, "", "/"); vi.useRealTimers(); });

describe("Vault settings section link (#650)", () => {
  it("scrolls to and briefly highlights the Protection section named by the link", async () => {
    window.history.replaceState({}, "", "/vaults/1/settings#protection");
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    renderVaultAccess();
    const protection = (await screen.findByRole("heading", { name: "Protection", level: 2 })).closest("section")!;
    expect(protection).toHaveAttribute("id", "protection");
    await waitFor(() => expect(protection).toHaveAttribute("data-highlighted", "true"));
    expect(scroll).toHaveBeenCalled();
    expect(scroll.mock.contexts[0]).toBe(protection);
    await waitFor(() => expect(protection).not.toHaveAttribute("data-highlighted"), { timeout: 5000 });
  }, 8000);

  it("highlights nothing without a fragment", async () => {
    renderVaultAccess();
    const protection = (await screen.findByRole("heading", { name: "Protection", level: 2 })).closest("section")!;
    expect(protection).not.toHaveAttribute("data-highlighted");
  });
});

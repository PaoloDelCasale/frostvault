import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AppShell } from "@/layout/AppShell";
import { loadCatalog } from "./vault-settings-harness";

describe.each(["en", "it"] as const)("essential sidebar %s", (locale) => {
  it("does not claim zero open problems when the global overview is unavailable", () => {
    const catalog = loadCatalog(locale); const t = (key: string) => catalog[key] ?? key;
    render(<AppShell capabilities={{ vaultName: "First", vaults: [], locale, locales: ["en", "it"] }} t={t}><p>Browser</p></AppShell>);
    expect(screen.getByRole("button", { name: `— ${t("essential.problems")}` })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: `0 ${t("essential.problems")}` })).not.toBeInTheDocument();
  });
  it("has protected folders, filters and General in the phone menu", async () => {
    const catalog = loadCatalog(locale); const t = (key: string) => catalog[key] ?? key;
    const choose = vi.fn(); const filter = vi.fn(); const general = vi.fn();
    render(<AppShell capabilities={{ vaultName: "First", vaults: [{ id: 1, name: "First", slug: "first" }], locale, locales: ["en", "it"] }} t={t} problemCounts={{ 1: 2 }} openProblems={2} handlers={{ onVaultChange: choose, onFilterChange: filter, onGeneral: general }}><p>Browser</p></AppShell>);
    const user = userEvent.setup(); await user.click(screen.getByRole("button", { name: t("ui.open_navigation") }));
    const menu = within(screen.getByRole("dialog", { name: t("ui.navigation") }));
    expect(menu.getByText(t("essential.protected_folders"))).toBeInTheDocument();
    expect(menu.getByText("● 2")).toBeInTheDocument();
    await user.click(menu.getByRole("button", { name: t("state.cloud_only") }));
    expect(filter).toHaveBeenCalledWith("cloud_only");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: `2 ${t("essential.problems")}` })).toBeInTheDocument();
  });
});

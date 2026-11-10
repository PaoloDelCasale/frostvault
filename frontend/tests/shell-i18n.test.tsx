import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AppShell } from "@/layout/AppShell";
import { loadCatalog } from "./vault-settings-harness";

const vaults = [{ id: 1, name: "First", slug: "first" }, { id: 2, name: "Second", slug: "second" }];

describe.each(["en", "it"] as const)("shell navigation %s", (locale) => {
  const catalog = loadCatalog(locale); const t = (key: string) => catalog[key] ?? key;

  it("does not claim zero open problems when the global overview is unavailable", () => {
    render(<AppShell capabilities={{ vaults: [], locale, locales: ["en", "it"] }} t={t}><p>Browser</p></AppShell>);
    expect(screen.queryByTestId("nav-problem-count")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: t("nav.problems") })).toHaveAttribute("href", "/problems");
  });

  it("has the fixed destinations, not the Vault list, in the phone menu", async () => {
    render(<AppShell capabilities={{ vaults, currentVaultId: 2, locale, locales: ["en", "it"] }} t={t} openProblems={2} section="files"><p>Browser</p></AppShell>);
    const user = userEvent.setup(); await user.click(screen.getByRole("button", { name: t("ui.open_navigation") }));
    const menu = within(screen.getByRole("dialog", { name: t("ui.navigation") }));
    expect(menu.getByRole("link", { name: t("nav.overview") })).toHaveAttribute("href", "/");
    expect(menu.getByRole("link", { name: t("nav.files") })).toHaveAttribute("aria-current", "page");
    expect(menu.getByRole("link", { name: t("nav.settings") })).toHaveAttribute("href", "/vaults/2/settings");
    expect(menu.getByRole("link", { name: t("nav.problems_open").replace("{count}", "2") })).toHaveAttribute("href", "/problems");
    expect(menu.getByRole("link", { name: t("nav.system") })).toHaveAttribute("href", "/system");
    expect(menu.queryByText("First")).not.toBeInTheDocument();
    await user.click(menu.getByRole("link", { name: t("nav.system") }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("offers Vaults and New Vault in the switcher, not a duplicate Overview entry (#523)", async () => {
    render(<AppShell capabilities={{ vaults, currentVaultId: 2, locale, locales: ["en", "it"] }} t={t}><p>Browser</p></AppShell>);
    await userEvent.setup().click(screen.getByTestId("vault-switcher"));
    const menu = within(await screen.findByRole("dialog", { name: t("nav.switch_vault") }));
    expect(menu.getByRole("button", { name: "First" })).toBeInTheDocument();
    expect(menu.getByRole("button", { name: t("nav.new_vault") })).toBeInTheDocument();
    expect(menu.getAllByRole("button")).toHaveLength(vaults.length + 1);
  });

  it("names the current Vault in the header switcher", () => {
    render(<AppShell capabilities={{ vaults, currentVaultId: 2, locale, locales: ["en", "it"] }} t={t}><p>Browser</p></AppShell>);
    expect(within(screen.getByRole("banner")).getByTestId("vault-switcher")).toHaveTextContent("Second");
  });
});

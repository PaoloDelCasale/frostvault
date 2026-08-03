import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities } from "@/layout/types";

async function openDrawer(
  capabilities: ShellCapabilities,
  t?: (key: string) => string,
) {
  const user = userEvent.setup();
  render(
    <AppShell capabilities={capabilities} t={t}>
      <p>content</p>
    </AppShell>,
  );
  await user.click(screen.getByRole("button", { name: /open navigation/i }));
  await screen.findByRole("dialog");
  return user;
}

describe("App drawer capability filtering", () => {
  it("shows Manage access for an owner, not Administration or Refresh list", async () => {
    await openDrawer({
      vaultName: "Owner Vault",
      isVaultOwner: true,
      canOperate: false,
      isAdmin: false,
      locale: "en",
      locales: ["en"],
      vaults: [{ id: 1, slug: "owner", name: "Owner Vault", role: "owner" }],
      role: "owner",
    });

    const drawer = screen.getByRole("dialog");
    expect(drawer).toHaveTextContent("Manage access");
    expect(drawer).not.toHaveTextContent("Administration");
    expect(drawer).not.toHaveTextContent("Refresh list");
    expect(drawer).toHaveTextContent("New vault");
    expect(drawer).toHaveTextContent("Sign out");
  });

  it("shows Refresh list for an operator, not Manage access or Administration", async () => {
    await openDrawer({
      vaultName: "Ops Vault",
      isVaultOwner: false,
      canOperate: true,
      isAdmin: false,
      locale: "en",
      locales: ["en"],
      vaults: [{ id: 2, slug: "ops", name: "Ops Vault", role: "operator" }],
      role: "operator",
    });

    const drawer = screen.getByRole("dialog");
    expect(drawer).toHaveTextContent("Refresh list");
    expect(drawer).not.toHaveTextContent("Manage access");
    expect(drawer).not.toHaveTextContent("Administration");
  });

  it("uses the translated Refresh list label for an operator", async () => {
    await openDrawer(
      {
        vaultName: "Ops Vault",
        isVaultOwner: false,
        canOperate: true,
        isAdmin: false,
        locale: "it",
        locales: ["it"],
        vaults: [{ id: 2, slug: "ops", name: "Ops Vault", role: "operator" }],
        role: "operator",
      },
      (key) => (key === "ui.refresh_list" ? "Aggiorna elenco" : key),
    );

    expect(screen.getByRole("dialog")).toHaveTextContent("Aggiorna elenco");
  });

  it("hides Manage access, Administration and Refresh list for a viewer", async () => {
    await openDrawer({
      vaultName: "View Vault",
      isVaultOwner: false,
      canOperate: false,
      isAdmin: false,
      locale: "en",
      locales: ["en"],
      vaults: [{ id: 3, slug: "view", name: "View Vault", role: "viewer" }],
      role: "viewer",
    });

    const drawer = screen.getByRole("dialog");
    expect(drawer).not.toHaveTextContent("Manage access");
    expect(drawer).not.toHaveTextContent("Administration");
    expect(drawer).not.toHaveTextContent("Refresh list");
    expect(drawer).toHaveTextContent("New vault");
    expect(drawer).toHaveTextContent("Language");
    expect(drawer).toHaveTextContent("Sign out");
  });

  it("shows Administration for an admin", async () => {
    await openDrawer({
      vaultName: "Admin Vault",
      isVaultOwner: false,
      canOperate: false,
      isAdmin: true,
      locale: "en",
      locales: ["en"],
      vaults: [{ id: 4, slug: "admin", name: "Admin Vault", role: "viewer" }],
      role: "viewer",
    });

    const drawer = screen.getByRole("dialog");
    expect(drawer).toHaveTextContent("Administration");
    expect(drawer).not.toHaveTextContent("Manage access");
    expect(drawer).not.toHaveTextContent("Refresh list");
  });
});

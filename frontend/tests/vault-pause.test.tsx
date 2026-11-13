import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests, setCsrfToken } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { OverviewPage } from "@/pages/overview/OverviewPage";
import { VaultSettingsPanel } from "@/pages/vault-settings/VaultSettingsPanel";
import { createVaultAccessFetch, jsonResponse } from "./vault-settings-harness";

afterEach(resetApiClientForTests);

describe("Persistent Vault pause controls (#537)", () => {
  function setup(page: "overview" | "settings", failed = false) {
    let paused = false;
    const posts: { url: string; init?: RequestInit }[] = [];
    const inventory = () => jsonResponse({ items: [{ id: 2, uuid: "photos", name: "Photos", paused, lifecycle: "active", source_root: "/sources/photos", s3_prefix: "photos" }] });
    const fetcher = createVaultAccessFetch({
      "GET /api/general/vaults": inventory,
      "GET /api/stats": () => jsonResponse({ states: { protected: 1 }, protection_state: "protected", storage: {} }),
      "POST /api/vaults/2/pause": (init) => {
        posts.push({ url: "/api/vaults/2/pause", init });
        if (failed) return jsonResponse({ detail: "Pause failed" }, 409);
        paused = true;
        return jsonResponse({ id: 2, paused, message: "Photos is paused until you resume it.", message_key: "vault_pause.paused_notice" });
      },
      "POST /api/vaults/2/resume": (init) => {
        posts.push({ url: "/api/vaults/2/resume", init }); paused = false;
        return jsonResponse({ id: 2, paused, message: "Photos is resumed.", message_key: "vault_pause.resumed_notice" });
      },
    });
    configureApiClient({ fetch: fetcher }); setCsrfToken("pause-csrf");
    render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider>{page === "overview"
      ? <OverviewPage vaults={[{ id: 2, uuid: "photos", name: "Photos" }]} />
      : <VaultSettingsPanel vaultId={2} vaultName="Photos" onStopped={vi.fn()} />}</I18nProvider></ApiQueryProvider>);
    return posts;
  }

  it.each(["overview", "settings"] as const)("pauses/resumes in %s, shows separate badge and CSRF POST without cancellation", async (page) => {
    const posts = setup(page);
    const user = userEvent.setup();
    const scope = page === "overview" ? within(await screen.findByTestId("overview-table")) : within(await screen.findByTestId("vault-settings"));
    const button = await scope.findByRole("button", { name: "Pause cloud work" });
    await waitFor(() => expect(button).toBeEnabled());
    await user.click(button);
    expect(await scope.findByText("Paused Vault")).toBeInTheDocument();
    expect(posts[0].url).toBe("/api/vaults/2/pause");
    expect(new Headers(posts[0].init?.headers).get("X-CSRF-Token")).toBe("pause-csrf");
    if (page === "overview") expect(scope.getByText("Protected")).toBeInTheDocument();
    await user.click(scope.getByRole("button", { name: "Resume cloud work" }));
    await waitFor(() => expect(scope.queryByText("Paused Vault")).not.toBeInTheDocument());
    expect(posts.map((p) => p.url)).toEqual(["/api/vaults/2/pause", "/api/vaults/2/resume"]);
  });

  it("failed pause keeps authoritative status and shows the error", async () => {
    setup("settings", true);
    const button = await screen.findByRole("button", { name: "Pause cloud work" });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.setup().click(button);
    expect(await screen.findByText("Pause failed")).toBeInTheDocument();
    expect(screen.queryByText("Paused Vault")).not.toBeInTheDocument();
    expect(button).toBeEnabled();
  });
});
